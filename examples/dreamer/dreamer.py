import dataclasses
import uuid
from datetime import datetime
from pathlib import Path

import hydra
import torch
import torch.cuda
import tqdm
from dreamer_utils import (
    parallel_env_constructor,
    transformed_env_constructor,
    call_record,
    grad_norm,
    make_recorder_env,
    EnvConfig,
)
from hydra.core.config_store import ConfigStore

# float16
from torch.cuda.amp import autocast, GradScaler
from torch.nn.utils import clip_grad_norm_
from torchrl.modules.tensordict_module.exploration import (
    AdditiveGaussianWrapper,
    OrnsteinUhlenbeckProcessWrapper,
)
from torchrl.objectives.dreamer import (
    DreamerActorLoss,
    DreamerModelLoss,
    DreamerValueLoss,
)
from torchrl.trainers.helpers.collectors import (
    make_collector_offpolicy,
    OffPolicyCollectorConfig,
)
from torchrl.trainers.helpers.envs import (
    correct_for_frame_skip,
    get_stats_random_rollout,
)
from torchrl.trainers.helpers.logger import LoggerConfig
from torchrl.trainers.helpers.models import (
    make_dreamer,
    DreamerConfig,
)
from torchrl.trainers.helpers.replay_buffer import (
    make_replay_buffer,
    ReplayArgsConfig,
)
from torchrl.trainers.helpers.trainers import TrainerConfig
from torchrl.trainers.trainers import Recorder, RewardNormalizer


config_fields = [
    (config_field.name, config_field.type, config_field)
    for config_cls in (
        OffPolicyCollectorConfig,
        EnvConfig,
        LoggerConfig,
        ReplayArgsConfig,
        DreamerConfig,
        TrainerConfig,
    )
    for config_field in dataclasses.fields(config_cls)
]
Config = dataclasses.make_dataclass(cls_name="Config", fields=config_fields)
cs = ConfigStore.instance()
cs.store(name="config", node=Config)


@hydra.main(version_base=None, config_path=".", config_name="config")
def main(cfg: "DictConfig"):  # noqa: F821

    cfg = correct_for_frame_skip(cfg)

    if not isinstance(cfg.reward_scaling, float):
        cfg.reward_scaling = 1.0

    if torch.cuda.is_available() and not cfg.model_device != "":
        device = torch.device("cuda:0")
    elif cfg.model_device:
        device = torch.device(cfg.model_device)
    else:
        device = torch.device("cpu")
    print(f"Using device {device}")
    exp_name = "_".join(
        [
            "Dreamer",
            cfg.exp_name,
            str(uuid.uuid4())[:8],
            datetime.now().strftime("%y_%m_%d-%H_%M_%S"),
        ]
    )

    if cfg.logger == "wandb":
        from torchrl.trainers.loggers.wandb import WandbLogger

        logger = WandbLogger(
            f"dreamer/{exp_name}",
            project="torchrl",
            group=f"Dreamer_{cfg.env_name}",
            offline=cfg.offline_logging,
        )
    elif cfg.logger == "csv":
        from torchrl.trainers.loggers.csv import CSVLogger

        logger = CSVLogger(
            f"{exp_name}",
            log_dir="dreamer",
        )
    elif cfg.logger == "tensorboard":
        from torchrl.trainers.loggers.tensorboard import TensorboardLogger

        logger = TensorboardLogger(
            f"{exp_name}",
            log_dir="dreamer",
        )
    else:
        raise NotImplementedError(cfg.logger)

    video_tag = f"Dreamer_{cfg.env_name}_policy_test" if cfg.record_video else ""

    stats = None

    # Compute the stats of the observations
    if not cfg.vecnorm and cfg.norm_stats:
        stats = get_stats_random_rollout(
            cfg,
            proof_environment=transformed_env_constructor(cfg)(),
            key=("next", "pixels")
            if cfg.from_pixels
            else ("next", "observation_vector"),
        )
        stats = {k: v.clone() for k, v in stats.items()}
    elif cfg.from_pixels:
        stats = {"loc": 0.5, "scale": 0.5}

    # Create the different components of dreamer
    world_model, model_based_env, actor_model, value_model, policy = make_dreamer(
        stats=stats,
        cfg=cfg,
        device=device,
        use_decoder_in_env=True,
        action_key="action",
        value_key="state_value",
        proof_environment=transformed_env_constructor(cfg)(),
    )

    # reward normalization
    if cfg.normalize_rewards_online:
        # if used the running statistics of the rewards are computed and the
        # rewards used for training will be normalized based on these.
        reward_normalizer = RewardNormalizer(
            scale=cfg.normalize_rewards_online_scale,
            decay=cfg.normalize_rewards_online_decay,
        )
    else:
        reward_normalizer = None

    # Losses
    world_model_loss = DreamerModelLoss(world_model)
    actor_loss = DreamerActorLoss(
        actor_model,
        value_model,
        model_based_env,
        imagination_horizon=cfg.imagination_horizon,
    )
    value_loss = DreamerValueLoss(value_model)

    # Exploration noise to be added to the actions
    if cfg.exploration == "additive_gaussian":
        exploration_policy = AdditiveGaussianWrapper(
            policy,
            sigma_init=0.3,
            sigma_end=0.3,
        ).to(device)
    elif cfg.exploration == "ou_exploration":
        exploration_policy = OrnsteinUhlenbeckProcessWrapper(
            policy,
            annealing_num_steps=cfg.total_frames,
        ).to(device)
    elif cfg.exploration == "":
        exploration_policy = policy.to(device)

    action_dim_gsde, state_dim_gsde = None, None
    create_env_fn = parallel_env_constructor(
        cfg=cfg,
        stats=stats,
        action_dim_gsde=action_dim_gsde,
        state_dim_gsde=state_dim_gsde,
    )

    # Create the replay buffer

    collector = make_collector_offpolicy(
        make_env=create_env_fn,
        actor_model_explore=exploration_policy,
        cfg=cfg,
        # make_env_kwargs=[
        #     {"device": device}
        #     for device in cfg.collector_devices
        # ],
    )
    print("collector:", collector)

    replay_buffer = make_replay_buffer(device, cfg)

    record = Recorder(
        record_frames=cfg.record_frames,
        frame_skip=cfg.frame_skip,
        policy_exploration=policy,
        recorder=make_recorder_env(
            cfg,
            video_tag,
            stats,
            logger,
            create_env_fn,
        ),
        record_interval=cfg.record_interval,
        log_keys=cfg.recorder_log_keys,
    )

    final_seed = collector.set_seed(cfg.seed)
    print(f"init seed: {cfg.seed}, final seed: {final_seed}")
    # Training loop
    collected_frames = 0
    pbar = tqdm.tqdm(total=cfg.total_frames)
    path = Path("./log")
    path.mkdir(exist_ok=True)

    # optimizers
    world_model_opt = torch.optim.Adam(world_model.parameters(), lr=cfg.world_model_lr)
    actor_opt = torch.optim.Adam(actor_model.parameters(), lr=cfg.actor_value_lr)
    value_opt = torch.optim.Adam(value_model.parameters(), lr=cfg.actor_value_lr)

    scaler1 = GradScaler()
    scaler2 = GradScaler()
    scaler3 = GradScaler()

    for i, tensordict in enumerate(collector):
        cmpt = 0
        if reward_normalizer is not None:
            reward_normalizer.update_reward_stats(tensordict)
        pbar.update(tensordict.numel())
        current_frames = tensordict.numel()
        collected_frames += current_frames

        # Compared to the original paper, the replay buffer is not temporally sampled. We fill it with trajectories of length batch_length.
        # To be closer to the paper, we would need to fill it with trajectories of lentgh 1000 and then sample subsequences of length batch_length.

        # tensordict = tensordict.reshape(-1, cfg.batch_length)
        print(tensordict.shape)
        replay_buffer.extend(tensordict.cpu())
        logger.log_scalar(
            "r_training",
            tensordict["reward"].mean().detach().item(),
            step=collected_frames,
        )

        if (i % cfg.record_interval) == 0:
            do_log = True
        else:
            do_log = False

        if collected_frames >= cfg.init_random_frames:
            if i % cfg.record_interval == 0:
                logger.log_scalar("cmpt", cmpt)
            for j in range(cfg.optim_steps_per_batch):
                cmpt += 1
                # sample from replay buffer
                sampled_tensordict = replay_buffer.sample(cfg.batch_size).to(
                    device, non_blocking=True
                )
                if reward_normalizer is not None:
                    sampled_tensordict = reward_normalizer.normalize_reward(
                        sampled_tensordict
                    )
                # update world model
                with autocast(dtype=torch.float16):
                    model_loss_td, sampled_tensordict = world_model_loss(
                        sampled_tensordict
                    )
                    loss_world_model = (
                        model_loss_td["loss_model_kl"]
                        + model_loss_td["loss_model_reco"]
                        + model_loss_td["loss_model_reward"]
                    )
                    # If we are logging videos, we keep some frames.
                    if (
                        cfg.record_video
                        and (record._count + 1) % cfg.record_interval == 0
                    ):
                        sampled_tensordict_save = (
                            sampled_tensordict.select(
                                "next" "state",
                                "belief",
                            )[:4]
                            .detach()
                            .to_tensordict()
                        )
                    else:
                        sampled_tensordict_save = None

                    scaler1.scale(loss_world_model).backward()
                    scaler1.unscale_(world_model_opt)
                    clip_grad_norm_(world_model.parameters(), cfg.grad_clip)
                    scaler1.step(world_model_opt)
                    if j == cfg.optim_steps_per_batch - 1 and do_log:
                        logger.log_scalar(
                            "loss_world_model",
                            loss_world_model.detach().item(),
                            step=collected_frames,
                        )
                        logger.log_scalar(
                            "grad_world_model",
                            grad_norm(world_model_opt),
                            step=collected_frames,
                        )
                        logger.log_scalar(
                            "loss_model_kl",
                            model_loss_td["loss_model_kl"].detach().item(),
                            step=collected_frames,
                        )
                        logger.log_scalar(
                            "loss_model_reco",
                            model_loss_td["loss_model_reco"].detach().item(),
                            step=collected_frames,
                        )
                        logger.log_scalar(
                            "loss_model_reward",
                            model_loss_td["loss_model_reward"].detach().item(),
                            step=collected_frames,
                        )
                    world_model_opt.zero_grad()
                    scaler1.update()

                # update actor network
                with autocast(dtype=torch.float16):
                    actor_loss_td, sampled_tensordict = actor_loss(sampled_tensordict)
                scaler2.scale(actor_loss_td["loss_actor"]).backward()
                scaler2.unscale_(actor_opt)
                clip_grad_norm_(actor_model.parameters(), cfg.grad_clip)
                scaler2.step(actor_opt)
                if j == cfg.optim_steps_per_batch - 1 and do_log:
                    logger.log_scalar(
                        "loss_actor",
                        actor_loss_td["loss_actor"].detach().item(),
                        step=collected_frames,
                    )
                    logger.log_scalar(
                        "grad_actor",
                        grad_norm(actor_opt),
                        step=collected_frames,
                    )
                actor_opt.zero_grad()
                scaler2.update()

                # update value network
                with autocast(dtype=torch.float16):
                    value_loss_td, sampled_tensordict = value_loss(sampled_tensordict)
                scaler3.scale(value_loss_td["loss_value"]).backward()
                scaler3.unscale_(value_opt)
                clip_grad_norm_(value_model.parameters(), cfg.grad_clip)
                scaler3.step(value_opt)
                if j == cfg.optim_steps_per_batch - 1 and do_log:
                    logger.log_scalar(
                        "loss_value",
                        value_loss_td["loss_value"].detach().item(),
                        step=collected_frames,
                    )
                    logger.log_scalar(
                        "grad_value",
                        grad_norm(value_opt),
                        step=collected_frames,
                    )
                value_opt.zero_grad()
                scaler3.update()
                if j == cfg.optim_steps_per_batch - 1:
                    do_log = False

            call_record(
                logger,
                record,
                collected_frames,
                sampled_tensordict_save,
                stats,
                model_based_env,
                actor_model,
                cfg,
            )
        if cfg.exploration != "":
            exploration_policy.step(current_frames)
        collector.update_policy_weights_()


if __name__ == "__main__":
    main()
