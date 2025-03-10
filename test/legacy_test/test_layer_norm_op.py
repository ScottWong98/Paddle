#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
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

import unittest
from functools import reduce
from operator import mul

import numpy as np
from op_test import OpTest, _set_use_system_allocator, convert_float_to_uint16

import paddle
import paddle.nn.functional as F
from paddle import base
from paddle.base import Program, core, program_guard
from paddle.static.amp.fp16_utils import _keep_layer_norm_scale_bias_to_fp32

paddle.enable_static()

np.random.seed(123)
paddle.seed(123)

_set_use_system_allocator(True)


def _reference_layer_norm_naive(x, scale, beta, epsilon, begin_norm_axis=1):
    x_shape = x.shape
    N = reduce(mul, x_shape[0:begin_norm_axis], 1)
    D = reduce(mul, x_shape[begin_norm_axis : len(x_shape)], 1)
    x.shape = [N, D]

    mean = np.mean(x, axis=1)
    var = np.var(x, axis=1) + epsilon
    output = np.divide(
        (x - mean.reshape([N, 1])), (np.sqrt(var)).reshape([N, 1])
    )
    if scale is not None:
        output = scale.reshape([1, D]) * output
    if beta is not None:
        output = output + beta.reshape([1, D])

    x.shape, output.shape = x_shape, x_shape
    return output, mean, var


def _reference_layer_norm_grad(
    x, grad_y, scale, bias, mean, var, begin_norm_axis=1
):
    x_shape = x.shape
    N = reduce(mul, x_shape[0:begin_norm_axis], 1)
    D = reduce(mul, x_shape[begin_norm_axis : len(x_shape)], 1)

    if scale is not None:
        scale_shape = scale.shape
        scale.shape = [1, D]
    x.shape, grad_y.shape = [N, D], [N, D]
    var.shape, mean.shape = [N, 1], [N, 1]

    # d_bias
    if bias is not None:
        d_bias = np.sum(grad_y, axis=0).reshape([1, D])
    else:
        d_bias = None
    # d_scale
    if scale is not None:
        d_scale = np.sum(
            ((x - mean) * np.sqrt(1 / var)) * grad_y, axis=0
        ).reshape([1, D])
    else:
        d_scale = None
    # dx
    if scale is not None:
        dx_end = scale * np.sqrt(1.0 / var) * grad_y
        d_mean_0 = np.sum(-np.sqrt(1.0 / var) * grad_y * scale, axis=1).reshape(
            [N, 1]
        )  # the second part equals to zero.
        d_mean = 1.0 / D * d_mean_0
        d_std = np.sum(
            -(1.0 / var) * (x - mean) * grad_y * scale, axis=1
        ).reshape([N, 1]) * (
            1.0 / D * np.sqrt(1.0 / var).reshape([N, 1]) * (x - mean)
        )
    else:
        dx_end = 1.0 * np.sqrt(1.0 / var) * grad_y
        d_mean_0 = np.sum(-np.sqrt(1.0 / var) * grad_y * 1.0, axis=1).reshape(
            [N, 1]
        )  # the second part equals to zero.
        d_mean = 1.0 / D * d_mean_0
        d_std = np.sum(
            -(1.0 / var) * (x - mean) * grad_y * 1.0, axis=1
        ).reshape([N, 1]) * (
            1.0 / D * np.sqrt(1.0 / var).reshape([N, 1]) * (x - mean)
        )

    grad_x = dx_end + d_mean + d_std

    grad_x.shape, x.shape, grad_y.shape = x_shape, x_shape, x_shape
    var.shape, mean.shape = [N], [N]

    if scale is not None:
        scale.shape = scale_shape
    return grad_x, d_scale, d_bias


def layer_norm_wrapper(
    x, scale=None, bias=None, epsilon=1e-05, begin_norm_axis=1
):
    input_shape = list(x.shape)
    normalized_shape = input_shape[begin_norm_axis:]
    return paddle.nn.functional.layer_norm(
        x, normalized_shape, weight=scale, bias=bias, epsilon=epsilon
    )


@unittest.skipIf(
    paddle.is_compiled_with_rocm(),
    "ROCm doesn't support fp64 LayerNormOpByOp currently",
)
class TestLayerNormOpByOpTest(OpTest):
    def setUp(self):
        self.python_api = layer_norm_wrapper
        self.public_python_api = layer_norm_wrapper
        self.op_type = "layer_norm"
        self.prim_op_type = "comp"
        self.python_out_sig = ["Y"]
        self.initConfig()
        self.initTestCase()

    def test_check_output(self):
        self.check_output(
            no_check_set=["Mean", "Variance"],
            atol=self.ori_atol,
            rtol=self.ori_rtol,
            check_prim=self.check_prim,
            check_prim_pir=self.check_prim_pir,
            check_new_ir=self.check_new_ir,
        )

    def test_check_grad(self):
        self.check_grad(
            self.check_grad_input_list,
            ['Y'],
            max_relative_error=self.max_relative_error,
            check_prim=self.check_prim,
            check_prim_pir=self.check_prim_pir,
            check_new_ir=self.check_new_ir,
        )

    def initConfig(self):
        self.rev_comp_atol = 1e-7
        self.rev_comp_rtol = 1e-7
        self.fw_comp_atol = 1e-6
        self.fw_comp_rtol = 1e-6

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.cinn_atol = 1e-5
        self.cinn_rtol = 1e-5

        self.max_relative_error = 1e-5
        # ROCm does not have float64 LayerNorm kernel
        self.dtype = "float64"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = True
        self.check_prim = True
        self.check_prim_pir = True
        self.check_new_ir = True

    def initTestCase(self):
        np.random.seed(123)

        self.D = reduce(
            mul, self.x_shape[self.begin_norm_axis : len(self.x_shape)], 1
        )
        self.scale_shape = [self.D]
        x = np.random.random(self.x_shape).astype(self.dtype)
        scale = (
            np.random.random(self.scale_shape).astype(self.dtype)
            if self.has_scale
            else None
        )
        bias = (
            np.random.random(self.scale_shape).astype(self.dtype)
            if self.has_bias
            else None
        )
        self.inputs = {
            "X": x,
        }
        self.check_grad_input_list = ['X']

        if self.has_scale:
            self.inputs.update({"Scale": scale})
            self.check_grad_input_list.append('Scale')
        if self.has_bias:
            self.inputs.update({"Bias": bias})
            self.check_grad_input_list.append('Bias')

        self.attrs = {
            "epsilon": self.epsilon,
            "begin_norm_axis": self.begin_norm_axis,
        }
        y, mean, variance = _reference_layer_norm_naive(
            x, scale, bias, self.epsilon, self.begin_norm_axis
        )
        self.outputs = {
            "Y": y,
            "Mean": mean,
            "Variance": variance,
        }


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestLayerNormBF16OpByOpTest(OpTest):
    def setUp(self):
        self.python_api = layer_norm_wrapper
        self.public_python_api = layer_norm_wrapper
        self.op_type = "layer_norm"
        self.prim_op_type = "comp"
        self.python_out_sig = ["Y"]
        self.initConfig()
        self.initTestCase()

    def test_check_output(self):
        self.check_output_with_place(
            place=core.CUDAPlace(0),
            no_check_set=["Mean", "Variance"],
            atol=self.ori_atol,
            rtol=self.ori_rtol,
            check_prim=self.check_prim,
            check_prim_pir=self.check_prim_pir,
            check_new_ir=self.check_new_ir,
        )

    def test_check_grad(self):
        self.check_grad_with_place(
            core.CUDAPlace(0),
            self.check_grad_input_list,
            ['Y'],
            max_relative_error=self.max_relative_error,
            check_prim=self.check_prim,
            check_prim_pir=self.check_prim_pir,
            check_new_ir=self.check_new_ir,
        )

    def initConfig(self):
        self.ori_atol = 1e-2
        self.ori_rtol = 1e-2

        self.max_relative_error = 1e-5

        self.dtype = np.uint16
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = True
        self.check_prim = True
        self.check_prim_pir = True
        self.check_new_ir = True

    def initTestCase(self):
        np.random.seed(123)

        self.D = reduce(
            mul, self.x_shape[self.begin_norm_axis : len(self.x_shape)], 1
        )
        self.scale_shape = [self.D]
        x = np.random.random(self.x_shape).astype("float32")
        scale = (
            np.random.random(self.scale_shape).astype("float32")
            if self.has_scale
            else None
        )
        bias = (
            np.random.random(self.scale_shape).astype("float32")
            if self.has_bias
            else None
        )
        self.inputs = {
            "X": convert_float_to_uint16(x),
        }
        self.check_grad_input_list = ['X']

        if self.has_scale:
            self.inputs.update({"Scale": convert_float_to_uint16(scale)})
            self.check_grad_input_list.append('Scale')
        if self.has_bias:
            self.inputs.update({"Bias": convert_float_to_uint16(bias)})
            self.check_grad_input_list.append('Bias')

        self.attrs = {
            "epsilon": self.epsilon,
            "begin_norm_axis": self.begin_norm_axis,
        }
        y, mean, variance = _reference_layer_norm_naive(
            x, scale, bias, self.epsilon, self.begin_norm_axis
        )
        self.outputs = {
            "Y": convert_float_to_uint16(y),
            "Mean": convert_float_to_uint16(mean),
            "Variance": convert_float_to_uint16(variance),
        }


@unittest.skipIf(
    paddle.is_compiled_with_rocm(),
    "ROCm doesn't support fp64 LayerNormOpByOp currently",
)
class TestLayerNormOpByOpTestFP64_case2(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-6
        self.rev_comp_rtol = 1e-6
        self.fw_comp_atol = 1e-7
        self.fw_comp_rtol = 1e-7

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.cinn_atol = 1e-5
        self.cinn_rtol = 1e-5

        self.max_relative_error = 1e-5

        self.dtype = "float64"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestLayerNormBF16OpByOpTest_case2(TestLayerNormBF16OpByOpTest):
    def initConfig(self):
        self.ori_atol = 1e-2
        self.ori_rtol = 1e-2

        self.max_relative_error = 1e-5

        self.dtype = np.uint16
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


@unittest.skipIf(
    paddle.is_compiled_with_rocm(),
    "ROCm doesn't support fp64 LayerNormOpByOp currently",
)
class TestLayerNormOpByOpTestFP64_case3(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-7
        self.rev_comp_rtol = 1e-7
        self.fw_comp_atol = 1e-7
        self.fw_comp_rtol = 1e-7

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.cinn_atol = 1e-5
        self.cinn_rtol = 1e-5

        self.max_relative_error = 1e-5

        self.dtype = "float64"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestLayerNormBF16OpByOpTest_case3(TestLayerNormBF16OpByOpTest):
    def initConfig(self):
        self.ori_atol = 1e-2
        self.ori_rtol = 1e-2

        self.max_relative_error = 1e-5

        self.dtype = np.uint16
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


@unittest.skipIf(
    paddle.is_compiled_with_rocm(),
    "ROCm doesn't support fp64 LayerNormOpByOp currently",
)
class TestLayerNormOpByOpTestFP64_case4(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-6
        self.rev_comp_rtol = 1e-6
        self.fw_comp_atol = 1e-7
        self.fw_comp_rtol = 1e-7

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.cinn_atol = 1e-5
        self.cinn_rtol = 1e-5

        self.max_relative_error = 1e-5

        self.dtype = "float64"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = True
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestLayerNormBF16OpByOpTest_case4(TestLayerNormBF16OpByOpTest):
    def initConfig(self):
        self.ori_atol = 1e-2
        self.ori_rtol = 1e-2

        self.max_relative_error = 1e-5

        self.dtype = np.uint16
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = True
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


class TestLayerNormOpByOpTestFP32(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-5
        self.rev_comp_rtol = 1e-5

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.max_relative_error = 7e-3

        self.dtype = "float32"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = True
        self.check_prim = True
        self.check_prim_pir = True
        self.check_new_ir = True


class TestLayerNormOpByOpTestFP32_case2(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-5
        self.rev_comp_rtol = 1e-5

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.max_relative_error = 1e-5

        self.dtype = "float32"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


class TestLayerNormOpByOpTestFP32_case3(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-5
        self.rev_comp_rtol = 1e-5

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.max_relative_error = 3e-3

        self.dtype = "float32"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = True
        self.has_bias = False
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


class TestLayerNormOpByOpTestFP32_case4(TestLayerNormOpByOpTest):
    def initConfig(self):
        self.rev_comp_atol = 1e-5
        self.rev_comp_rtol = 1e-5

        self.ori_atol = 1e-4
        self.ori_rtol = 1e-4
        self.max_relative_error = 1e-3

        self.dtype = "float32"
        self.x_shape = [2, 6, 6, 3]
        self.epsilon = 0.00001
        self.begin_norm_axis = 1
        self.has_scale = False
        self.has_bias = True
        self.check_prim = False
        self.check_prim_pir = False
        self.check_new_ir = True


class TestLayerNormOp(unittest.TestCase):
    def setUp(self):
        self.use_cudnn = True
        paddle.enable_static()

    def __assert_close(self, tensor, np_array, msg, atol=1e-4):
        np.testing.assert_allclose(
            np.array(tensor).flatten(),
            np_array.flatten(),
            rtol=1e-3,
            atol=atol,
            err_msg=msg,
        )

    def check_forward_backward(
        self,
        shape,
        begin_norm_axis,
        has_scale=True,
        has_bias=True,
        y_grad_scale=1.0,
        use_mkldnn=False,
    ):
        def test_with_place(
            place, shape, begin_norm_axis, use_mkldnn=use_mkldnn
        ):
            # attr
            epsilon = 0.00001
            x_shape = shape
            D = reduce(mul, x_shape[begin_norm_axis : len(x_shape)], 1)
            scale_shape = [D]

            np.random.seed(123)
            x = np.random.random_sample(x_shape).astype(np.float32)
            scale = (
                np.random.random_sample(scale_shape).astype(np.float32)
                if has_scale
                else None
            )
            bias = (
                np.random.random_sample(scale_shape).astype(np.float32)
                if has_bias
                else None
            )
            y_grad = (np.random.random_sample(x_shape) * y_grad_scale).astype(
                np.float32
            )

            # reference forward & backward
            y, mean, variance = _reference_layer_norm_naive(
                x, scale, bias, epsilon, begin_norm_axis
            )
            x_grad, scale_grad, bias_grad = _reference_layer_norm_grad(
                x, y_grad, scale, bias, mean, variance, begin_norm_axis
            )

            var_dict = locals()
            var_dict['y@GRAD'] = y_grad
            var_names = ['x', 'mean', 'variance', 'y', 'y@GRAD']
            if has_scale:
                var_names += ['scale']
            if has_bias:
                var_names += ['bias']
            ground_truth = {name: var_dict[name] for name in var_names}

            program = base.Program()
            with base.program_guard(program):
                block = program.global_block()
                for name in ground_truth:
                    block.create_var(
                        name=name,
                        dtype='float32',
                        shape=ground_truth[name].shape,
                    )
                inputs = {"X": block.var('x')}
                fetch_list = [
                    'y',
                    'mean',
                    'variance',
                    'x@GRAD',
                ]
                if has_scale:
                    inputs["Scale"] = block.var('scale')
                    fetch_list += ['scale@GRAD']
                if has_bias:
                    inputs["Bias"] = block.var('bias')
                    fetch_list += ['bias@GRAD']
                layer_norm_op = block.append_op(
                    type="layer_norm",
                    inputs=inputs,
                    outputs={
                        "Y": block.var('y'),
                        "Mean": block.var('mean'),  # share the same memory
                        "Variance": block.var(
                            'variance'
                        ),  # share the same memory
                    },
                    attrs={
                        "epsilon": epsilon,
                        "begin_norm_axis": begin_norm_axis,
                        "use_mkldnn": use_mkldnn,
                    },
                )
                # generate backward op_desc
                grad_op_desc_list, op_grad_to_var = core.get_grad_op_desc(
                    layer_norm_op.desc, set(), []
                )
                grad_op_desc = grad_op_desc_list[0]
                new_op_desc = block.desc.append_op()
                new_op_desc.copy_from(grad_op_desc)
                for var_name in grad_op_desc.output_arg_names():
                    block.desc.var(var_name.encode("ascii"))
                grad_op_desc.infer_var_type(block.desc)
                grad_op_desc.infer_shape(block.desc)
                for arg in grad_op_desc.output_arg_names():
                    grad_var = block.desc.find_var(arg.encode("ascii"))
                    grad_var.set_dtype(core.VarDesc.VarType.FP32)

                program._sync_with_cpp()
                exe = base.Executor(place)
                name_list = ['x', 'y@GRAD']
                if has_scale:
                    name_list += ['scale']
                if has_bias:
                    name_list += ['bias']

                out = exe.run(
                    program,
                    feed={name: var_dict[name] for name in name_list},
                    fetch_list=fetch_list,
                )
                # print(y)
                # print(out[0])
                self.__assert_close(y, out[0], "y")
                self.__assert_close(mean, out[1], "mean")
                self.__assert_close(variance, out[2], "variance", 1e-3)
                self.__assert_close(x_grad, out[3], "x_grad")
                if has_scale:
                    self.__assert_close(
                        scale_grad,
                        out[fetch_list.index('scale@GRAD')],
                        "scale_grad",
                        1e-3,
                    )
                if has_bias:
                    self.__assert_close(
                        bias_grad,
                        out[fetch_list.index('bias@GRAD')],
                        "bias_grad",
                    )

        places = [core.CPUPlace()]
        if (
            core.is_compiled_with_cuda()
            and core.op_support_gpu("layer_norm")
            and self.use_cudnn
        ):
            places.append(core.CUDAPlace(0))

        for place in places:
            test_with_place(place, shape, begin_norm_axis)

    def test_check_forward_backward_with_scale_and_bias(self):
        self.check_forward_backward(shape=[2, 3, 4, 5], begin_norm_axis=1)
        self.check_forward_backward(shape=[1, 3, 4, 5], begin_norm_axis=1)
        self.check_forward_backward(
            shape=[2, 3, 4, 5],
            begin_norm_axis=1,
            has_scale=False,
            has_bias=True,
        )
        self.check_forward_backward(
            shape=[2, 3, 4, 5],
            begin_norm_axis=1,
            has_scale=True,
            has_bias=False,
        )
        self.check_forward_backward(
            shape=[2, 3, 4, 5],
            begin_norm_axis=1,
            has_scale=False,
            has_bias=False,
        )
        self.check_forward_backward(shape=[2, 3, 4, 5], begin_norm_axis=3)
        self.check_forward_backward(
            shape=[92, 513, 129], begin_norm_axis=2, y_grad_scale=0.1
        )
        self.check_forward_backward(shape=[3, 34, 1134], begin_norm_axis=2)
        self.check_forward_backward(shape=[3, 2, 1133], begin_norm_axis=2)
        self.check_forward_backward(
            shape=[92, 513, 1134], begin_norm_axis=2, y_grad_scale=0.1
        )
        self.check_forward_backward(
            shape=[92, 513, 1134],
            begin_norm_axis=2,
            has_scale=False,
            has_bias=True,
            y_grad_scale=0.1,
        )
        self.check_forward_backward(
            shape=[92, 513, 1134],
            begin_norm_axis=2,
            has_scale=True,
            has_bias=False,
            y_grad_scale=0.1,
        )
        self.check_forward_backward(
            shape=[92, 513, 1134],
            begin_norm_axis=2,
            has_scale=False,
            has_bias=False,
            y_grad_scale=0.1,
        )
        self.check_forward_backward(
            shape=[512, 1024], begin_norm_axis=1, has_scale=True, has_bias=True
        )
        self.check_forward_backward(
            shape=[1, 128, 256, 256],
            begin_norm_axis=3,
            has_scale=True,
            has_bias=True,
        )
        self.check_forward_backward(
            shape=[1, 256, 384],
            begin_norm_axis=2,
            has_scale=True,
            has_bias=True,
        )


class TestLayerNormAPI(unittest.TestCase):
    def test_case(self):
        x = paddle.static.data(name='x', shape=[64, 32, 256], dtype='float32')
        x = paddle.static.nn.layer_norm(
            x,
            scale=True,
            shift=True,
            begin_norm_axis=1,
            epsilon=1e-05,
            param_attr=None,
            bias_attr=None,
        )
        x = paddle.static.nn.layer_norm(
            x,
            scale=False,
            shift=False,
            begin_norm_axis=1,
            epsilon=1e-05,
            param_attr=None,
            bias_attr=None,
        )
        x = paddle.static.nn.layer_norm(
            x,
            scale=True,
            shift=True,
            begin_norm_axis=1,
            epsilon=1e-05,
            param_attr="scale",
            bias_attr="shift",
        )


class TestDygraphLayerNormAPIError(unittest.TestCase):
    def test_errors(self):
        with program_guard(Program(), Program()):
            paddle.enable_static()

            layer_norm = paddle.nn.LayerNorm([32, 32])
            # the input of LayerNorm must be Variable.
            x1 = np.random.random((3, 32, 32)).astype('float32')
            self.assertRaises(TypeError, layer_norm, x1)

            # the input dtype of LayerNorm must be float32 or float64
            # float16 only can be set on GPU place
            x2 = paddle.static.data(
                name='x2', shape=[-1, 3, 32, 32], dtype="int32"
            )
            self.assertRaises(TypeError, layer_norm, x2)
        with paddle.pir_utils.IrGuard(), program_guard(Program(), Program()):
            layer_norm = paddle.nn.LayerNorm([32, 32])
            # the input of LayerNorm must be Variable.
            x1 = np.random.random((3, 32, 32)).astype('float32')
            self.assertRaises(ValueError, layer_norm, x1)


@unittest.skipIf(
    not core.is_compiled_with_cuda(),
    "core is not compiled with CUDA or not support the float16",
)
class TestFP16ScaleBiasLayerNorm(unittest.TestCase):
    def check_main(self, x_np, weight_np, bias_np, dtype):
        paddle.disable_static()

        weight_np = weight_np.astype(dtype)
        bias_np = bias_np.astype(dtype)

        x = paddle.to_tensor(x_np)
        weight = paddle.to_tensor(weight_np)
        bias = paddle.to_tensor(bias_np)
        x.stop_gradient = False
        weight.stop_gradient = False
        bias.stop_gradient = False
        y = F.layer_norm(x, x.shape[1:], weight, bias)
        x_g, w_g, b_g = paddle.grad(y, [x, weight, bias])
        y_np = y.numpy().astype('float32')
        x_g_np = x_g.numpy().astype('float32')
        w_g_np = w_g.numpy().astype('float16')
        b_g_np = b_g.numpy().astype('float32')

        paddle.enable_static()
        return y_np, x_g_np, w_g_np, b_g_np

    def test_main(self):
        x_np = np.random.random([10, 20]).astype('float16')
        weight_np = np.random.random([20]).astype('float16')
        bias_np = np.random.random([20]).astype('float16')

        y_np_1, x_g_np_1, w_g_np_1, b_g_np_1 = self.check_main(
            x_np, weight_np, bias_np, 'float16'
        )
        y_np_2, x_g_np_2, w_g_np_2, b_g_np_2 = self.check_main(
            x_np, weight_np, bias_np, 'float32'
        )

        def assert_equal(x, y):
            np.testing.assert_array_equal(x, y)

        assert_equal(y_np_1, y_np_2)
        assert_equal(x_g_np_1, x_g_np_2)
        assert_equal(w_g_np_1, w_g_np_2)
        assert_equal(b_g_np_1, b_g_np_2)


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestBF16ScaleBiasLayerNorm(unittest.TestCase):
    def check_main(self, x_np, weight_np, bias_np, dtype):
        paddle.disable_static()

        x = paddle.to_tensor(x_np)
        weight = paddle.to_tensor(weight_np)
        bias = paddle.to_tensor(bias_np)

        if dtype == "bfloat16":
            x = x.cast(paddle.base.core.VarDesc.VarType.BF16)

        x.stop_gradient = False
        weight.stop_gradient = False
        bias.stop_gradient = False

        y = F.layer_norm(x, x.shape[1:], weight, bias)
        x_g, w_g, b_g = paddle.grad(y, [x, weight, bias])

        y_np = y.cast('float32').numpy()
        x_g_np = x_g.cast('float32').numpy()
        w_g_np = w_g.cast('float32').numpy()
        b_g_np = b_g.cast('float32').numpy()

        paddle.enable_static()
        return y_np, x_g_np, w_g_np, b_g_np

    def test_main(self):
        x_np = np.random.random([10, 20]).astype('float32')
        weight_np = np.random.random([20]).astype('float32')
        bias_np = np.random.random([20]).astype('float32')

        y_np_1, x_g_np_1, w_g_np_1, b_g_np_1 = self.check_main(
            x_np, weight_np, bias_np, 'float32'
        )
        y_np_2, x_g_np_2, w_g_np_2, b_g_np_2 = self.check_main(
            x_np, weight_np, bias_np, 'bfloat16'
        )

        def assert_equal(x, y):
            np.testing.assert_allclose(x, y, rtol=1e-05, atol=3e-2)

        assert_equal(y_np_1, y_np_2)
        assert_equal(x_g_np_1, x_g_np_2)
        assert_equal(w_g_np_1, w_g_np_2)
        assert_equal(b_g_np_1, b_g_np_2)


class TestGetSetKeepLayerNormScaleBiasFP32Flag(unittest.TestCase):
    def test_main(self):
        self.assertTrue(_keep_layer_norm_scale_bias_to_fp32())
        _keep_layer_norm_scale_bias_to_fp32(False)
        self.assertFalse(_keep_layer_norm_scale_bias_to_fp32())
        _keep_layer_norm_scale_bias_to_fp32(True)
        self.assertTrue(_keep_layer_norm_scale_bias_to_fp32())


@unittest.skipIf(
    not core.is_compiled_with_cuda() or paddle.is_compiled_with_rocm(),
    "core is not compiled with CUDA or not support the FastMath",
)
class TestFastMathLayerNormOp(unittest.TestCase):
    def check_layer_norm(
        self, dtype, x_np, scale_np, bias_np, norm_axis, has_scale, has_bias
    ):
        paddle.disable_static()
        epsilon = 0.00001

        x = paddle.to_tensor(x_np)
        if dtype == "bfloat16":
            x = x.cast(paddle.base.core.VarDesc.VarType.BF16)

        x.stop_gradient = True
        bias = paddle.to_tensor(bias_np) if has_scale else None
        scale = paddle.to_tensor(scale_np) if has_bias else None
        if bias is not None:
            bias.stop_gradient = True
        if scale is not None:
            scale.stop_gradient = True

        y = F.layer_norm(x, x.shape[norm_axis:], scale, bias)
        y_np = y.cast('float32').numpy()
        paddle.enable_static()
        return y_np

    def check_with_fast_math(
        self, dtype, shape, norm_axis, has_scale, has_bias
    ):
        def use_fast_math(enabled):
            paddle.set_flags({'FLAGS_use_fast_math': enabled})

        def __assert_close(x, y):
            np.testing.assert_allclose(x, y, rtol=1e-05, atol=1e-04)

        x_np = np.random.random(shape).astype('float32')
        bias_np = np.random.random(shape[norm_axis:]).astype('float32')
        scale_np = np.random.random(shape[norm_axis:]).astype('float32')

        use_fast_math(False)
        y_fast = self.check_layer_norm(
            dtype, x_np, scale_np, bias_np, norm_axis, has_scale, has_bias
        )
        use_fast_math(True)
        y_dev = self.check_layer_norm(
            dtype, x_np, scale_np, bias_np, norm_axis, has_scale, has_bias
        )
        __assert_close(y_fast, y_dev)

    def check_with_dtype(self, dtype):
        self.check_with_fast_math(
            dtype,
            shape=[17, 129],
            norm_axis=1,
            has_scale=False,
            has_bias=True,
        )
        self.check_with_fast_math(
            dtype,
            shape=[8, 512],
            norm_axis=1,
            has_scale=False,
            has_bias=False,
        )
        self.check_with_fast_math(
            dtype,
            shape=[2, 768],
            norm_axis=1,
            has_scale=False,
            has_bias=False,
        )

    def init_dtype(self):
        self.dtype = 'float32'

    def test_main(self):
        self.init_dtype()
        self.check_with_dtype(dtype=self.dtype)


@unittest.skipIf(
    not core.is_compiled_with_cuda()
    or paddle.is_compiled_with_rocm()
    or not core.is_bfloat16_supported(core.CUDAPlace(0)),
    "core is not compiled with CUDA or not support the bfloat16",
)
class TestFastMathLayerNormBF16Op(TestFastMathLayerNormOp):
    def init_dtype(self):
        self.dtype = 'bfloat16'


if __name__ == '__main__':
    paddle.enable_static()
    unittest.main()
