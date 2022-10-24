# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from torchrl.modules.distributions import NormalParamWrapper, Delta
from torchrl.modules.tensordict_module.actors import ProbabilisticActor
from torchrl.modules.tensordict_module.common import TensorDictModule
from torchrl.modules.tensordict_module.probabilistic import (
    ProbabilisticTensorDictModule,
)

__all__ = [
    "partial_tensordictmodule",
    "partial_probabilisticactor",
    "partial_probabilistictensordictmodule",
]

_WRAPPER_DICT = {
    "normal_param": NormalParamWrapper,
}


def partial_tensordictmodule(
    partial_module, in_keys, out_keys, spec=None, safe=False, wrapper=None, **kwargs
):
    """Creates a partially instantiated :obj:`TensorDictModule`.

    Args:
        partial_module (partially instantiated :obj:`nn.Module`): a module to instantiate using
            the kwargs provided to this function.
        in_keys (iterable of strings): in_keys of the :obj:`TensorDictModule`.
        out_keys (iterable of strings): out_keys of the :obj:`TensorDictModule`.
        spec (TensorSpec, optional): the optional TensorSpec to pass to the
            TensorDictModule.
        safe (bool, optional): safe arg for the :obj:`TensorDictModule`. Default: :obj:`False`.
        wrapper (module wrapper): an optional module wrapper, such as
            :obj:`NormalParamWrapper` (indicated by :obj:`"normal_param"`).

    """
    module = partial_module(**kwargs)
    if wrapper is not None:
        module = _WRAPPER_DICT[wrapper](module)

    return TensorDictModule(
        module=module,
        in_keys=in_keys,
        out_keys=out_keys,
        spec=spec,
        safe=safe,
    )


def partial_probabilisticactor(
    partial_tensordictmodule,
    dist_param_keys,
    out_key_sample=None,
    spec=None,
    safe=False,
    default_interaction_mode="mode",
    distribution_class=Delta,
    distribution_kwargs=None,
    return_log_prob=False,
    n_empirical_estimate: int = 1000,
    **kwargs,
):
    """Creates a partially instantiated :obj:`ProbabilisticActor`.

    Args:
        partial_tensordictmodule (partially instantiated :obj:`TensorDictModule`):
            a module to instantiate using the kwargs provided to this function.
        dist_param_keys (iterable of strings): :obj:`dist_param_keys` of the
            :obj:`ProbabilisticActor`.
        **kwargs: optional arguments of the :obj:`ProbabilisticActor`.
    """
    return ProbabilisticActor(
        module=partial_tensordictmodule(**kwargs),
        dist_param_keys=dist_param_keys,
        out_key_sample=out_key_sample,
        spec=spec,
        safe=safe,
        default_interaction_mode=default_interaction_mode,
        distribution_class=distribution_class,
        distribution_kwargs=distribution_kwargs,
        return_log_prob=return_log_prob,
        n_empirical_estimate=n_empirical_estimate,
    )


def partial_probabilistictensordictmodule(
    partial_tensordictmodule,
    dist_param_keys,
    out_key_sample=None,
    spec=None,
    safe=False,
    default_interaction_mode="mode",
    distribution_class=Delta,
    distribution_kwargs=None,
    return_log_prob=False,
    n_empirical_estimate: int = 1000,
    **kwargs,
):
    """Creates a partially instantiated :obj:`ProbabilisticTensorDictModule`.

    Args:
        partial_tensordictmodule (partially instantiated :obj:`TensorDictModule`):
            a module to instantiate using the kwargs provided to this function.
        dist_param_keys (iterable of strings): :obj:`dist_param_keys` of the
            :obj:`ProbabilisticActor`.
        **kwargs: optional arguments of the :obj:`ProbabilisticTensorDictModule`.
    """

    return ProbabilisticTensorDictModule(
        module=partial_tensordictmodule(**kwargs),
        dist_param_keys=dist_param_keys,
        out_key_sample=out_key_sample,
        spec=spec,
        safe=safe,
        default_interaction_mode=default_interaction_mode,
        distribution_class=distribution_class,
        distribution_kwargs=distribution_kwargs,
        return_log_prob=return_log_prob,
        n_empirical_estimate=n_empirical_estimate,
    )
