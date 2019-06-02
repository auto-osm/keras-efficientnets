# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
"""Contains definitions for EfficientNet model.
[1] Mingxing Tan, Quoc V. Le
  EfficientNet: Rethinking Model Scaling for Convolutional Neural Networks.
  ICML'19, https://arxiv.org/abs/1905.11946
"""
import math
from typing import List

import numpy as np
import tensorflow as tf
from keras import backend as K
from keras import layers
from keras.models import Model
from keras.utils import get_file, get_source_inputs
from keras_applications.imagenet_utils import _obtain_input_shape

from config import BlockArgs, DEFAULT_BLOCK_LIST
from layers import Swish, DropConnect

__all__ = ['EfficientNet',
           'EfficientNetB0',
           'EfficientNetB1',
           'EfficientNetB2',
           'EfficientNetB3',
           'EfficientNetB4',
           'EfficientNetB5',
           'EfficientNetB6',
           'EfficientNetB7',
           'preprocess_input']


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/main.py#L243-L244
_IMAGENET_MEAN = np.array([0.485 * 255, 0.456 * 255, 0.406 * 255])
_IMAGENET_STDDEV = np.array([0.229 * 255, 0.224 * 255, 0.225 * 255])


def preprocess_input(x, data_format=None):
    assert x.ndim == 3
    assert x.shape[-1] == 3

    if data_format is None:
        data_format = K.image_data_format()

    if data_format == 'channels_first':
        mean = _IMAGENET_MEAN.reshape((3, 1, 1))
        std = _IMAGENET_STDDEV.reshape((3, 1, 1))
    else:
        mean = _IMAGENET_MEAN.reshape((1, 1, 3))
        std = _IMAGENET_STDDEV.reshape((1, 1, 3))

    x = x - mean
    x = x / std

    return x


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def conv_kernel_initializer(shape, dtype=K.floatx(), partition_info=None):
    """Initialization for convolutional kernels.
    The main difference with tf.variance_scaling_initializer is that
    tf.variance_scaling_initializer uses a truncated normal with an uncorrected
    standard deviation, whereas here we use a normal distribution. Similarly,
    tf.contrib.layers.variance_scaling_initializer uses a truncated normal with
    a corrected standard deviation.
    Args:
      shape: shape of variable
      dtype: dtype of variable
      partition_info: unused
    Returns:
      an initialization for the variable
    """
    del partition_info
    kernel_height, kernel_width, _, out_filters = shape
    fan_out = int(kernel_height * kernel_width * out_filters)
    return tf.random_normal(
        shape, mean=0.0, stddev=np.sqrt(2.0 / fan_out), dtype=dtype)


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def dense_kernel_initializer(shape, dtype=K.floatx(), partition_info=None):
    """Initialization for dense kernels.
    This initialization is equal to
      tf.variance_scaling_initializer(scale=1.0/3.0, mode='fan_out',
                                      distribution='uniform').
    It is written out explicitly here for clarity.
    Args:
      shape: shape of variable
      dtype: dtype of variable
      partition_info: unused
    Returns:
      an initialization for the variable
    """
    del partition_info
    init_range = 1.0 / np.sqrt(shape[1])
    return tf.random_uniform(shape, -init_range, init_range, dtype=dtype)


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def round_filters(filters, width_coefficient, depth_divisor, min_depth):
    """Round number of filters based on depth multiplier."""
    orig_f = filters
    multiplier = width_coefficient
    divisor = depth_divisor
    min_depth = min_depth

    if not multiplier:
        return filters

    filters *= multiplier
    min_depth = min_depth or divisor
    new_filters = max(min_depth, int(filters + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_filters < 0.9 * filters:
        new_filters += divisor

    return int(new_filters)


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def round_repeats(repeats, depth_coefficient):
    """Round number of filters based on depth multiplier."""
    multiplier = depth_coefficient

    if not multiplier:
        return repeats

    return int(math.ceil(multiplier * repeats))


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def SEBlock(input_filters, se_ratio, expand_ratio, data_format=None):
    if data_format is None:
        data_format = K.image_data_format()

    num_reduced_filters = max(
        1, int(input_filters * se_ratio))
    filters = input_filters * expand_ratio

    if data_format == 'channels_first':
        channel_axis = 1
        spatial_dims = [2, 3]
    else:
        channel_axis = -1
        spatial_dims = [1, 2]

    def block(inputs):
        x = inputs
        x = layers.Lambda(lambda a: K.mean(a, axis=spatial_dims, keepdims=True))(x)
        x = layers.Conv2D(
            num_reduced_filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=True)(x)
        x = Swish()(x)
        # Excite
        x = layers.Conv2D(
            filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=True)(x)
        x = layers.Activation('sigmoid')(x)
        out = layers.Multiply()([x, inputs])
        return out

    return block


# Obtained from https://github.com/tensorflow/tpu/blob/master/models/official/efficientnet/efficientnet_model.py
def MBConvBlock(input_filters, output_filters,
                kernel_size, strides,
                expand_ratio, se_ratio,
                id_skip, drop_connect_rate,
                batch_norm_momentum=0.99,
                batch_norm_epsilon=1e-3,
                data_format=None):

    if data_format is None:
        data_format = K.image_data_format()

    if data_format == 'channels_first':
        channel_axis = 1
        spatial_dims = [2, 3]
    else:
        channel_axis = -1
        spatial_dims = [1, 2]

    has_se = (se_ratio is not None) and (se_ratio > 0) and (se_ratio <= 1)
    filters = input_filters * expand_ratio

    def block(inputs):

        if expand_ratio != 1:
            x = layers.Conv2D(
                filters,
                kernel_size=[1, 1],
                strides=[1, 1],
                kernel_initializer=conv_kernel_initializer,
                padding='same',
                use_bias=False)(inputs)
            x = layers.BatchNormalization(
                axis=channel_axis,
                momentum=batch_norm_momentum,
                epsilon=batch_norm_epsilon)(x)
            x = Swish()(x)
        else:
            x = inputs

        x = layers.DepthwiseConv2D(
            [kernel_size, kernel_size],
            strides=strides,
            depthwise_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=False)(x)
        x = layers.BatchNormalization(
            axis=channel_axis,
            momentum=batch_norm_momentum,
            epsilon=batch_norm_epsilon)(x)
        x = Swish()(x)

        if has_se:
            x = SEBlock(input_filters, se_ratio, expand_ratio,
                        data_format)(x)

        # output phase

        x = layers.Conv2D(
            output_filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=False)(x)
        x = layers.BatchNormalization(
            axis=channel_axis,
            momentum=batch_norm_momentum,
            epsilon=batch_norm_epsilon)(x)

        if id_skip:
            if all(s == 1 for s in strides) and (
                    input_filters == output_filters):

                # only apply drop_connect if skip presents.
                if drop_connect_rate:
                    x = DropConnect(drop_connect_rate)(x)

                x = layers.Add()([x, inputs])

        return x

    return block


def EfficientNet(input_shape,
                 block_args_list: List[BlockArgs],
                 width_coefficient: float,
                 depth_coefficient: float,
                 include_top=True,
                 weights=None,
                 input_tensor=None,
                 pooling=None,
                 classes=1000,
                 dropout_rate=0.,
                 drop_connect_rate=0.,
                 batch_norm_momentum=0.99,
                 batch_norm_epsilon=1e-3,
                 depth_divisor=8,
                 min_depth=None,
                 data_format=None,
                 default_size=None,
                 **kwargs):

    if data_format is None:
        data_format = K.image_data_format()

    if data_format == 'channels_first':
        channel_axis = 1
    else:
        channel_axis = -1

    if default_size is None:
        default_size = 224

    # count number of strides to compute min size
    stride_count = 1
    for block_args in block_args_list:
        if block_args.strides is not None and block_args.strides[0] > 1:
            stride_count += 1

    min_size = int(2 ** stride_count)

    # Determine proper input shape and default size.
    input_shape = _obtain_input_shape(input_shape,
                                      default_size=default_size,
                                      min_size=min_size,
                                      data_format=data_format,
                                      require_flatten=include_top,
                                      weights=weights)

    # Stem part
    if input_tensor is None:
        inputs = layers.Input(shape=input_shape)
    else:
        if not K.is_keras_tensor(input_tensor):
            inputs = layers.Input(tensor=input_tensor, shape=input_shape)
        else:
            inputs = input_tensor

    x = inputs
    x = layers.Conv2D(
        filters=round_filters(32, width_coefficient,
                              depth_divisor, min_depth),
        kernel_size=[3, 3],
        strides=[2, 2],
        kernel_initializer=conv_kernel_initializer,
        padding='same',
        use_bias=False)(x)
    x = layers.BatchNormalization(
        axis=channel_axis,
        momentum=batch_norm_momentum,
        epsilon=batch_norm_epsilon)(x)
    x = Swish()(x)

    # Blocks part
    for block_args in block_args_list:
        assert block_args.num_repeat > 0

        # Update block input and output filters based on depth multiplier.
        block_args.input_filters = round_filters(block_args.input_filters, width_coefficient, depth_divisor, min_depth)
        block_args.output_filters = round_filters(block_args.output_filters, width_coefficient, depth_divisor, min_depth)
        block_args.num_repeat = round_repeats(block_args.num_repeat, depth_coefficient)

        # The first block needs to take care of stride and filter size increase.
        x = MBConvBlock(block_args.input_filters, block_args.output_filters,
                        block_args.kernel_size, block_args.strides,
                        block_args.expand_ratio, block_args.se_ratio,
                        block_args.identity_skip, drop_connect_rate,
                        batch_norm_momentum, batch_norm_epsilon, data_format)(x)

        if block_args.num_repeat > 1:
            block_args.input_filters = block_args.output_filters
            block_args.strides = [1, 1]

        for _ in range(block_args.num_repeat - 1):
            x = MBConvBlock(block_args.input_filters, block_args.output_filters,
                            block_args.kernel_size, block_args.strides,
                            block_args.expand_ratio, block_args.se_ratio,
                            block_args.identity_skip, drop_connect_rate,
                            batch_norm_momentum, batch_norm_epsilon, data_format)(x)

    # Head part
    x = layers.Conv2D(
        filters=round_filters(1280, width_coefficient, depth_coefficient, min_depth),
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=conv_kernel_initializer,
        padding='same',
        use_bias=False)(x)
    x = layers.BatchNormalization(
        axis=channel_axis,
        momentum=batch_norm_momentum,
        epsilon=batch_norm_epsilon)(x)
    x = Swish()(x)

    if include_top:
        x = layers.GlobalAveragePooling2D(data_format=data_format)(x)

        if dropout_rate > 0:
            x = layers.Dropout(dropout_rate)(x)

        x = layers.Dense(classes, kernel_initializer=dense_kernel_initializer)(x)
        x = layers.Activation('softmax')(x)

    else:
        if pooling == 'avg':
            x = layers.GlobalAveragePooling2D()(x)
        elif pooling == 'max':
            x = layers.GlobalMaxPooling2D()(x)

    outputs = x

    # Ensure that the model takes into account
    # any potential predecessors of `input_tensor`.
    if input_tensor is not None:
        inputs = get_source_inputs(input_tensor)

    model = Model(inputs, outputs)

    # Load weights
    if weights == 'imagenet':
        if default_size == 224:
            if include_top:
                weights_path = get_file(
                    'efficientnet-b0.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b0.h5",
                    cache_subdir='models')
            else:
                weights_path = get_file(
                    'efficientnet-b0_notop.h5.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b0_notop.h5",
                    cache_subdir='models')
            model.load_weights(weights_path)

        elif default_size == 240:
            if include_top:
                weights_path = get_file(
                    'efficientnet-b1.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b1.h5",
                    cache_subdir='models')
            else:
                weights_path = get_file(
                    'efficientnet-b1_notop.h5.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b1_notop.h5",
                    cache_subdir='models')
            model.load_weights(weights_path)

        elif default_size == 260:
            if include_top:
                weights_path = get_file(
                    'efficientnet-b2.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b2.h5",
                    cache_subdir='models')
            else:
                weights_path = get_file(
                    'efficientnet-b2_notop.h5.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b2_notop.h5",
                    cache_subdir='models')
            model.load_weights(weights_path)

        elif default_size == 300:
            if include_top:
                weights_path = get_file(
                    'efficientnet-b3.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b3.h5",
                    cache_subdir='models')
            else:
                weights_path = get_file(
                    'efficientnet-b3_notop.h5',
                    "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b3_notop.h5",
                    cache_subdir='models')
            model.load_weights(weights_path)

        # elif default_size == 380:
        #     if include_top:
        #         weights_path = get_file(
        #             'efficientnet-b4.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b4.h5",
        #             cache_subdir='models')
        #     else:
        #         weights_path = get_file(
        #             'efficientnet-b4_notoph5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b4_notop.h5",
        #             cache_subdir='models')
        #     model.load_weights(weights_path)
        #
        # elif default_size == 456:
        #     if include_top:
        #         weights_path = get_file(
        #             'efficientnet-b5.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b5.h5",
        #             cache_subdir='models')
        #     else:
        #         weights_path = get_file(
        #             'efficientnet-b5_notop.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b5_notop.h5",
        #             cache_subdir='models')
        #     model.load_weights(weights_path)
        #
        # elif default_size == 528:
        #     if include_top:
        #         weights_path = get_file(
        #             'efficientnet-b6.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b6.h5",
        #             cache_subdir='models')
        #     else:
        #         weights_path = get_file(
        #             'efficientnet-b6_notop.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b6_notop.h5",
        #             cache_subdir='models')
        #     model.load_weights(weights_path)
        #
        # elif default_size == 600:
        #     if include_top:
        #         weights_path = get_file(
        #             'efficientnet-b7.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b7.h5",
        #             cache_subdir='models')
        #     else:
        #         weights_path = get_file(
        #             'efficientnet-b7_notop.h5',
        #             "https://github.com/titu1994/keras-efficientnets/releases/download/v0.1/efficientnet-b7_notop.h5",
        #             cache_subdir='models')
        #     model.load_weights(weights_path)

        else:
            raise ValueError('ImageNet weights can only be loaded with EfficientNetB0-7')

    elif weights is not None:
        model.load_weights(weights)

    return model


def EfficientNetB0(input_shape=None,
                   include_top=True,
                   weights='imagenet',
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.2,
                   drop_connect_rate=0.,
                   data_format=None):

    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.0,
                        depth_coefficient=1.0,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=224)


def EfficientNetB1(input_shape=None,
                   include_top=True,
                   weights='imagenet',
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.2,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.0,
                        depth_coefficient=1.1,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=240)


def EfficientNetB2(input_shape=None,
                   include_top=True,
                   weights='imagenet',
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.3,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.1,
                        depth_coefficient=1.2,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=260)


def EfficientNetB3(input_shape=None,
                   include_top=True,
                   weights='imagenet',
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.3,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.2,
                        depth_coefficient=1.4,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=300)


def EfficientNetB4(input_shape=None,
                   include_top=True,
                   weights=None,
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.4,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.4,
                        depth_coefficient=1.8,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=380)


def EfficientNetB5(input_shape=None,
                   include_top=True,
                   weights=None,
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.4,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.6,
                        depth_coefficient=2.2,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=456)


def EfficientNetB6(input_shape=None,
                   include_top=True,
                   weights=None,
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.5,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=1.8,
                        depth_coefficient=2.6,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=528)


def EfficientNetB7(input_shape=None,
                   include_top=True,
                   weights=None,
                   input_tensor=None,
                   pooling=None,
                   classes=1000,
                   dropout_rate=0.5,
                   drop_connect_rate=0.,
                   data_format=None):
    return EfficientNet(input_shape,
                        DEFAULT_BLOCK_LIST,
                        width_coefficient=2.0,
                        depth_coefficient=3.1,
                        include_top=include_top,
                        weights=weights,
                        input_tensor=input_tensor,
                        pooling=pooling,
                        classes=classes,
                        dropout_rate=dropout_rate,
                        drop_connect_rate=drop_connect_rate,
                        data_format=data_format,
                        default_size=600)


if __name__ == '__main__':
    model = EfficientNetB0(include_top=True)
    model.summary()