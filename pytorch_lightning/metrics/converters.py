# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This file provides functions and decorators for automated input and output
conversion to/from :class:`numpy.ndarray` and :class:`torch.Tensor` as well as utilities to
sync tensors between different processes in a DDP scenario, when needed.
"""

import numbers
from typing import Any, Callable, Optional, Union

import numpy as np
import torch
from torch.utils.data._utils.collate import np_str_obj_array_pattern

from pytorch_lightning.utilities import rank_zero_warn
from pytorch_lightning.utilities.apply_func import apply_to_collection

try:
    from torch.distributed import ReduceOp
except ImportError:
    class ReduceOp:
        SUM = None

    rank_zero_warn('Unsupported `ReduceOp` for distributed computing')


def _apply_to_inputs(func_to_apply: Callable, *dec_args, **dec_kwargs) -> Callable:
    """
    Decorator function to apply a function to all inputs of a function.

    Args:
        func_to_apply: the function to apply to the inputs
        *dec_args: positional arguments for the function to be applied
        **dec_kwargs: keyword arguments for the function to be applied

    Return:
        the decorated function
    """

    def decorator_fn(func_to_decorate):
        # actual function applying the give function to inputs
        def new_func(*args, **kwargs):
            args = func_to_apply(args, *dec_args, **dec_kwargs)
            kwargs = func_to_apply(kwargs, *dec_args, **dec_kwargs)
            return func_to_decorate(*args, **kwargs)

        return new_func

    return decorator_fn


def _apply_to_outputs(func_to_apply: Callable, *dec_args, **dec_kwargs) -> Callable:
    """
    Decorator function to apply a function to all outputs of a function.

    Args:
        func_to_apply: the function to apply to the outputs
        *dec_args: positional arguments for the function to be applied
        **dec_kwargs: keyword arguments for the function to be applied

    Return:
        the decorated function
    """

    def decorator_fn(function_to_decorate):
        # actual function applying the give function to outputs
        def new_func(*args, **kwargs):
            result = function_to_decorate(*args, **kwargs)
            return func_to_apply(result, *dec_args, **dec_kwargs)

        return new_func

    return decorator_fn


def convert_to_tensor(data: Any, dtype=None, device=None) -> Any:
    """
    Maps all kind of collections and numbers to tensors.

    Args:
        data: the data to convert to tensor
        dtype: data type to convert to
        device: device to cast to

    Return:
        the converted data
    """
    if isinstance(data, numbers.Number):
        return torch.tensor([data], dtype=dtype, device=device)
    # is not array of object
    elif isinstance(data, np.ndarray) and np_str_obj_array_pattern.search(data.dtype.str) is None:
        return torch.from_numpy(data).to(device=device, dtype=dtype)
    elif isinstance(data, torch.Tensor):
        return data.to(device=device, dtype=dtype)

    raise TypeError(f"The given type ('{type(data).__name__}') cannot be converted to a tensor!")


def convert_to_numpy(data: Union[torch.Tensor, np.ndarray, numbers.Number]) -> np.ndarray:
    """Convert all tensors and numpy arrays to numpy arrays.

    Args:
        data: the tensor or array to convert to numpy

    Return:
        the resulting numpy array
    """
    if isinstance(data, torch.Tensor):
        return data.cpu().detach().numpy()
    elif isinstance(data, numbers.Number):
        return np.array([data])
    elif isinstance(data, np.ndarray):
        return data

    raise TypeError("The given type ('%s') cannot be converted to a numpy array!" % type(data).__name__)


def _numpy_metric_input_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator converting all inputs of a function to numpy

    Args:
        func_to_decorate: the function whose inputs shall be converted

    Return:
        Callable: the decorated function
    """
    return _apply_to_inputs(
        apply_to_collection, (torch.Tensor, np.ndarray, numbers.Number), convert_to_numpy)(func_to_decorate)


def _tensor_metric_output_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator converting all outputs of a function to tensors

    Args:
        func_to_decorate: the function whose outputs shall be converted

    Return:
        Callable: the decorated function
    """
    return _apply_to_outputs(convert_to_tensor)(func_to_decorate)


def _numpy_metric_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator handling the argument conversion for metrics working on numpy.
    All inputs of the decorated function will be converted to numpy and all
    outputs will be converted to tensors.

    Args:
        func_to_decorate: the function whose inputs and outputs shall be converted

    Return:
        the decorated function
    """
    # applies collection conversion from tensor to numpy to all inputs
    # we need to include numpy arrays here, since otherwise they will also be treated as sequences
    func_convert_inputs = _numpy_metric_input_conversion(func_to_decorate)
    # converts all inputs back to tensors (device doesn't matter here, since this is handled by BaseMetric)
    func_convert_in_out = _tensor_metric_output_conversion(func_convert_inputs)
    return func_convert_in_out


def _tensor_metric_input_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator converting all inputs of a function to tensors

    Args:
        func_to_decorate: the function whose inputs shall be converted

    Return:
        Callable: the decorated function
    """
    return _apply_to_inputs(
        apply_to_collection, (torch.Tensor, np.ndarray, numbers.Number), convert_to_tensor)(func_to_decorate)


def _tensor_collection_metric_output_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator converting all numpy arrays and numbers occuring in the outputs of a function to tensors

    Args:
        func_to_decorate: the function whose outputs shall be converted

    Return:
        Callable: the decorated function
    """
    return _apply_to_outputs(apply_to_collection, (torch.Tensor, np.ndarray, numbers.Number),
                             convert_to_tensor)(func_to_decorate)


def _tensor_metric_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator Handling the argument conversion for metrics working on tensors.
    All inputs and outputs of the decorated function will be converted to tensors

    Args:
        func_to_decorate: the function whose inputs and outputs shall be converted

    Return:
        the decorated function
    """
    # converts all inputs to tensor if possible
    # we need to include tensors here, since otherwise they will also be treated as sequences
    func_convert_inputs = _tensor_metric_input_conversion(func_to_decorate)
    # convert all outputs to tensor if possible
    return _tensor_metric_output_conversion(func_convert_inputs)


def _tensor_collection_metric_conversion(func_to_decorate: Callable) -> Callable:
    """
    Decorator Handling the argument conversion for metrics working on tensors.
    All inputs of the decorated function and all numpy arrays and numbers in
    it's outputs will be converted to tensors

    Args:
        func_to_decorate: the function whose inputs and outputs shall be converted

    Return:
        the decorated function
    """
    # converts all inputs to tensor if possible
    # we need to include tensors here, since otherwise they will also be treated as sequences
    func_convert_inputs = _tensor_metric_input_conversion(func_to_decorate)
    # convert all outputs to tensor if possible
    return _tensor_collection_metric_output_conversion(func_convert_inputs)


def sync_ddp_if_available(result: Union[torch.Tensor],
                          group: Optional[Any] = None,
                          reduce_op: Optional[ReduceOp] = None
                          ) -> torch.Tensor:
    """
    Function to reduce the tensors from several ddp processes to one master process

    Args:
        result: the value to sync and reduce (typically tensor or number)
        group: the process group to gather results from. Defaults to all processes (world)
        reduce_op: the reduction operation. Defaults to sum.
            Can also be a string of 'avg', 'mean' to calculate the mean during reduction.

    Return:
        reduced value
    """

    if torch.distributed.is_available() and torch.distributed.is_initialized():
        divide_by_world_size = False

        if group is None:
            group = torch.distributed.group.WORLD

        if reduce_op is None:
            reduce_op = torch.distributed.ReduceOp.SUM
        elif isinstance(reduce_op, str) and reduce_op in ('avg', 'mean'):
            reduce_op = torch.distributed.ReduceOp.SUM
            divide_by_world_size = True

        # sync all processes before reduction
        torch.distributed.barrier(group=group)
        torch.distributed.all_reduce(result, op=reduce_op, group=group,
                                     async_op=False)

        if divide_by_world_size:
            result = result / torch.distributed.get_world_size(group)

    return result


def gather_all_tensors_if_available(result: Union[torch.Tensor],
                                    group: Optional[Any] = None):
    """
    Function to gather all tensors from several ddp processes onto a list that
    is broadcasted to all processes

    Args:
        result: the value to sync
        group: the process group to gather results from. Defaults to all processes (world)

    Return:
        gathered_result: list with size equal to the process group where
            gathered_result[i] corresponds to result tensor from process i

    """
    if torch.distributed.is_available() and torch.distributed.is_initialized():
        if group is None:
            group = torch.distributed.group.WORLD

        world_size = torch.distributed.get_world_size(group)

        gathered_result = [torch.zeros_like(result) for _ in range(world_size)]

        # sync and broadcast all
        torch.distributed.barrier(group=group)
        torch.distributed.all_gather(gathered_result, result, group)

        result = gathered_result

    return result


def sync_ddp(group: Optional[Any] = None,
             reduce_op: Optional[ReduceOp] = None) -> Callable:
    """
    This decorator syncs a functions outputs across different processes for DDP.

    Args:
        group: the process group to gather results from. Defaults to all processes (world)
        reduce_op: the reduction operation. Defaults to sum

    Return:
        the decorated function

    """

    def decorator_fn(func_to_decorate):
        return _apply_to_outputs(apply_to_collection, torch.Tensor,
                                 sync_ddp_if_available, group=group,
                                 reduce_op=reduce_op)(func_to_decorate)

    return decorator_fn


def numpy_metric(group: Optional[Any] = None,
                 reduce_op: Optional[ReduceOp] = None) -> Callable:
    """
    This decorator shall be used on all function metrics working on numpy arrays.
    It handles the argument conversion and DDP reduction for metrics working on numpy.
    All inputs of the decorated function will be converted to numpy and all
    outputs will be converted to tensors.
    In DDP Training all output tensors will be reduced according to the given rules.

    Args:
        group: the process group to gather results from. Defaults to all processes (world)
        reduce_op: the reduction operation. Defaults to sum

    Return:
        the decorated function
    """

    def decorator_fn(func_to_decorate):
        return sync_ddp(group=group, reduce_op=reduce_op)(_numpy_metric_conversion(func_to_decorate))

    return decorator_fn


def tensor_metric(group: Optional[Any] = None,
                  reduce_op: Optional[ReduceOp] = None) -> Callable:
    """
    This decorator shall be used on all function metrics working on tensors.
    It handles the argument conversion and DDP reduction for metrics working on tensors.
    All inputs and outputs of the decorated function will be converted to tensors.
    In DDP Training all output tensors will be reduced according to the given rules.

    Args:
       group: the process group to gather results from. Defaults to all processes (world)
       reduce_op: the reduction operation. Defaults to sum

    Return:
       the decorated function
    """

    def decorator_fn(func_to_decorate):
        return sync_ddp(group=group, reduce_op=reduce_op)(_tensor_metric_conversion(func_to_decorate))

    return decorator_fn


def tensor_collection_metric(group: Optional[Any] = None,
                             reduce_op: Optional[ReduceOp] = None) -> Callable:
    """
    This decorator shall be used on all function metrics working on tensors and returning collections
    that cannot be converted to tensors.
    It handles the argument conversion and DDP reduction for metrics working on tensors.
    All inputs and outputs of the decorated function will be converted to tensors.
    In DDP Training all output tensors will be reduced according to the given rules.

    Args:
       group: the process group to gather results from. Defaults to all processes (world)
       reduce_op: the reduction operation. Defaults to sum

    Return:
       the decorated function
    """

    def decorator_fn(func_to_decorate):
        return sync_ddp(group=group, reduce_op=reduce_op)(_tensor_collection_metric_conversion(func_to_decorate))

    return decorator_fn
