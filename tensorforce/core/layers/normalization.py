# Copyright 2020 Tensorforce Team. All Rights Reserved.
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
# ==============================================================================

import numpy as np
import tensorflow as tf

from tensorforce import TensorforceError, util
from tensorforce.core import parameter_modules, TensorSpec, tf_function, tf_util
from tensorforce.core.layers import Layer, StatefulLayer


class LinearNormalization(Layer):
    """
    Linear normalization layer which scales and shifts the input to [-2.0, 2.0], for bounded states
    with min/max_value (specification key: `linear_normalization`).

    Args:
        min_value (float | array[float]): Lower bound of the value
            (<span style="color:#00C000"><b>default</b></span>: based on input_spec).
        max_value (float | array[float]): Upper bound of the value range
            (<span style="color:#00C000"><b>default</b></span>: based on input_spec).
        name (string): Layer name
            (<span style="color:#00C000"><b>default</b></span>: internally chosen).
        input_spec (specification): <span style="color:#00C000"><b>internal use</b></span>.
    """

    def __init__(self, *, min_value=None, max_value=None, name=None, input_spec=None):
        if min_value is None:
            if input_spec.min_value is None:
                raise TensorforceError.required(name='LinearNormalization', argument='min_value')
            min_value = input_spec.min_value

        if max_value is None:
            if input_spec.max_value is None:
                raise TensorforceError.required(name='LinearNormalization', argument='max_value')
            max_value = input_spec.max_value

        self.min_value = np.asarray(min_value)
        self.max_value = np.asarray(max_value)

        if (self.min_value >= self.max_value).any():
            raise TensorforceError(
                name='LinearNormalization', argument='min/max_value',
                value=(self.min_value, self.max_value), hint='not less than'
            )

        super().__init__(name=name, input_spec=input_spec)

    def default_input_spec(self):
        return TensorSpec(
            type='float', shape=None, min_value=self.min_value, max_value=self.max_value
        )

    def output_spec(self):
        output_spec = super().output_spec()
        is_inf = np.logical_or(np.isinf(self.min_value), np.isinf(self.max_value))
        if is_inf.any():
            output_spec.min_value = np.where(is_inf, self.min_value, -2.0)
            output_spec.max_value = np.where(is_inf, self.max_value, 2.0)
        else:
            output_spec.min_value = -2.0
            output_spec.max_value = 2.0
        return output_spec

    @tf_function(num_args=1)
    def apply(self, *, x):
        is_inf = np.logical_or(np.isinf(self.min_value), np.isinf(self.max_value))
        is_inf = tf_util.constant(value=is_inf, dtype='bool')
        min_value = tf_util.constant(value=self.min_value, dtype='float')
        max_value = tf_util.constant(value=self.max_value, dtype='float')

        return tf.where(
            condition=is_inf, x=x, y=(4.0 * (x - min_value) / (max_value - min_value) - 2.0)
        )


class ExponentialNormalization(StatefulLayer):
    """
    Normalization layer based on the exponential moving average (specification key:
    `exponential_normalization`).

    Args:
        decay (parameter, 0.0 <= float <= 1.0): Decay rate
            (<span style="color:#00C000"><b>default</b></span>: 0.999).
        axes (iter[int >= 0]): Normalization axes, excluding batch axis
            (<span style="color:#00C000"><b>default</b></span>: all but last axis).
        l2_regularization (float >= 0.0): Scalar controlling L2 regularization
            (<span style="color:#00C000"><b>default</b></span>: inherit value of parent module).
        name (string): Layer name
            (<span style="color:#00C000"><b>default</b></span>: internally chosen).
        input_spec (specification): <span style="color:#00C000"><b>internal use</b></span>.
    """

    def __init__(self, *, decay=0.999, axes=None, name=None, input_spec=None):
        super().__init__(name=name, input_spec=input_spec)

        self.decay = self.submodule(
            name='decay', module=decay, modules=parameter_modules, dtype='float', min_value=0.0,
            max_value=1.0
        )

        self.axes = axes if axes is None else tuple(axes)

    def default_input_spec(self):
        return TensorSpec(type='float', shape=None)

    def initialize(self):
        super().initialize()

        shape = self.input_spec.shape
        if self.axes is None:
            if len(shape) > 0:
                self.axes = tuple(range(len(shape) - 1))
                shape = tuple(1 for _ in shape[:-1]) + (shape[-1],)
            else:
                self.axes = ()
        else:
            shape = tuple(1 if axis in self.axes else dims for axis, dims in enumerate(shape))
        shape = (1,) + shape

        self.moving_mean = self.variable(
            name='mean', spec=TensorSpec(type='float', shape=shape), initializer='zeros',
            is_trainable=False, is_saved=True
        )

        self.moving_variance = self.variable(
            name='variance', spec=TensorSpec(type='float', shape=shape), initializer='zeros',
            is_trainable=False, is_saved=True
        )

        self.after_first_call = self.variable(
            name='after-first-call', spec=TensorSpec(type='bool'), initializer='zeros',
            is_trainable=False, is_saved=True
        )

    @tf_function(num_args=1)
    def apply(self, *, x, independent):
        dependencies = list()

        if independent:
            mean = self.moving_mean
            variance = self.moving_variance

        else:
            one = tf_util.constant(value=1.0, dtype='float')
            axes = (0,) + tuple(1 + axis for axis in self.axes)

            decay = self.decay.value()
            batch_size = tf_util.cast(x=tf.shape(input=x)[0], dtype='float')
            decay = tf.math.pow(x=decay, y=batch_size)
            condition = tf.math.logical_or(
                x=self.after_first_call, y=tf.math.equal(x=batch_size, y=0)
            )

            mean = tf.math.reduce_mean(input_tensor=x, axis=axes, keepdims=True)
            mean = tf.where(
                condition=condition, x=(decay * self.moving_mean + (one - decay) * mean), y=mean
            )

            variance = tf.reduce_mean(
                input_tensor=tf.math.squared_difference(x=x, y=mean), axis=axes, keepdims=True
            )
            variance = tf.where(
                condition=condition, x=(decay * self.moving_variance + (one - decay) * variance),
                y=variance
            )

            with tf.control_dependencies(control_inputs=(mean, variance)):
                value = tf.math.logical_or(x=self.after_first_call, y=(batch_size > 0))
                dependencies.append(self.after_first_call.assign(value=value, read_value=False))

            mean = self.moving_mean.assign(value=mean)
            variance = self.moving_variance.assign(value=variance)

        epsilon = tf_util.constant(value=util.epsilon, dtype='float')
        reciprocal_stddev = tf.math.rsqrt(x=tf.maximum(x=variance, y=epsilon))

        with tf.control_dependencies(control_inputs=dependencies):
            x = (x - tf.stop_gradient(input=mean)) * tf.stop_gradient(input=reciprocal_stddev)

        return x


class InstanceNormalization(Layer):
    """
    Instance normalization layer (specification key: `instance_normalization`).

    Args:
        axes (iter[int >= 0]): Normalization axes, excluding batch axis
            (<span style="color:#00C000"><b>default</b></span>: all).
        name (string): Layer name
            (<span style="color:#00C000"><b>default</b></span>: internally chosen).
        input_spec (specification): <span style="color:#00C000"><b>internal use</b></span>.
    """

    def __init__(self, *, axes=None, name=None, input_spec=None):
        super().__init__(name=name, input_spec=input_spec)

        self.axes = axes if axes is None else tuple(axes)

    def default_input_spec(self):
        return TensorSpec(type='float', shape=None)

    @tf_function(num_args=1)
    def apply(self, *, x):
        epsilon = tf_util.constant(value=util.epsilon, dtype='float')

        if self.axes is None:
            mean, variance = tf.nn.moments(
                x=x, axes=tuple(range(1, self.input_spec.rank)), keepdims=True
            )
        else:
            mean, variance = tf.nn.moments(
                x=x, axes=tuple(1 + axis for axis in self.axes), keepdims=True
            )

        reciprocal_stddev = tf.math.rsqrt(x=tf.maximum(x=variance, y=epsilon))

        x = (x - tf.stop_gradient(input=mean)) * tf.stop_gradient(input=reciprocal_stddev)

        return x
