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

from collections import OrderedDict
import logging
import os
import time

import h5py
import numpy as np
import tensorflow as tf

from tensorforce import TensorforceError, util
from tensorforce.core import ArrayDict, Module, SignatureDict, TensorDict, TensorSpec, \
    TensorsSpec, tf_function, tf_util, VariableDict


class Model(Module):

    def __init__(
        self, *, states, actions, l2_regularization, parallel_interactions, config, saver,
        summarizer
    ):
        # Tensorforce config
        self._config = config

        Module._MODULE_STACK.clear()
        Module._MODULE_STACK.append(self.__class__)

        super().__init__(
            device=self._config.device, l2_regularization=l2_regularization, name=self._config.name
        )

        assert self.l2_regularization is not None
        self.is_trainable = True
        self.is_saved = True

        # Keep track of tensor names to check for collisions
        self.value_names = set()

        # Terminal specification
        self.terminal_spec = TensorSpec(type='int', shape=(), num_values=3)
        self.value_names.add('terminal')

        # Reward specification
        self.reward_spec = TensorSpec(type='float', shape=())
        self.value_names.add('reward')

        # Parallel specification
        self.parallel_spec = TensorSpec(type='int', shape=(), num_values=parallel_interactions)
        self.value_names.add('parallel')

        # State space specification
        self.states_spec = states
        for name, spec in self.states_spec.items():
            if spec.type != 'float':
                continue
            elif spec.min_value is None:
                logging.warning("No min_value bound specified for state {}.".format(name))
            elif np.isinf(spec.min_value).any():
                logging.warning("Infinite min_value bound for state {}.".format(name))
            elif spec.max_value is None:
                logging.warning("No max_value bound specified for state {}.".format(name))
            elif np.isinf(spec.max_value).any():
                logging.warning("Infinite max_value bound for state {}.".format(name))

        # Check for name collisions
        for name in self.states_spec:
            if name in self.value_names:
                raise TensorforceError.exists(name='value name', value=name)
            self.value_names.add(name)

        # Action space specification
        self.actions_spec = actions
        for name, spec in self.actions_spec.items():
            if spec.type != 'float':
                continue
            elif spec.min_value is None:
                logging.warning("No min_value specified for action {}.".format(name))
            elif np.isinf(spec.min_value).any():
                raise TensorforceError("Infinite min_value bound for action {}.".format(name))
            elif spec.max_value is None:
                logging.warning("No max_value specified for action {}.".format(name))
            elif np.isinf(spec.max_value).any():
                raise TensorforceError("Infinite max_value bound for action {}.".format(name))

        # Check for name collisions
        for name in self.actions_spec:
            if name in self.value_names:
                raise TensorforceError.exists(name='value name', value=name)
            self.value_names.add(name)

        # Internal state space specification
        self.internals_spec = TensorsSpec()
        self.internals_init = ArrayDict()

        # Auxiliary value space specification
        self.auxiliaries_spec = TensorsSpec()
        for name, spec in self.actions_spec.items():
            if self.config.enable_int_action_masking and spec.type == 'int' and \
                    spec.num_values is not None:
                self.auxiliaries_spec[name] = TensorsSpec(mask=TensorSpec(
                    type='bool', shape=(spec.shape + (spec.num_values,))
                ))

        # Parallel interactions
        assert isinstance(parallel_interactions, int) and parallel_interactions >= 1
        self.parallel_interactions = parallel_interactions

        # Saver
        if saver is None:
            self.saver = None
        elif not all(key in (
            'directory', 'filename', 'frequency', 'load', 'max_checkpoints', 'max_hour_frequency',
            'unit'
        ) for key in saver):
            raise TensorforceError.value(
                name='agent', argument='saver', value=list(saver),
                hint='not from {directory,filename,frequency,load,max_checkpoints,'
                     'max_hour_frequency,unit}'
            )
        elif 'directory' not in saver:
            raise TensorforceError.required(name='agent', argument='saver[directory]')
        elif 'frequency' not in saver:
            raise TensorforceError.required(name='agent', argument='saver[frequency]')
        else:
            self.saver = dict(saver)

        # Summarizer
        if summarizer is None:
            self.summarizer = None
            self.summary_labels = frozenset()
        elif not all(
            key in ('directory', 'flush', 'labels', 'max_summaries') for key in summarizer
        ):
            raise TensorforceError.value(
                name='agent', argument='summarizer', value=list(summarizer),
                hint='not from {directory,flush,labels,max_summaries}'
            )
        elif 'directory' not in summarizer:
            raise TensorforceError.required(name='agent', argument='summarizer[directory]')
        else:
            self.summarizer = dict(summarizer)

            # Summary labels
            summary_labels = summarizer.get('labels', ('graph',))
            if summary_labels == 'all':
                self.summary_labels = 'all'
            elif not all(isinstance(label, str) for label in summary_labels):
                raise TensorforceError.value(
                    name='agent', argument='summarizer[labels]', value=summary_labels
                )
            else:
                self.summary_labels = frozenset(summary_labels)

    @property
    def root(self):
        return self

    @property
    def config(self):
        return self._config

    def close(self):
        if self.saver is not None:
            self.save()
        if self.summarizer is not None:
            self.summarizer.close()

    def __enter__(self):
        assert self.is_initialized is not None
        if self.is_initialized:
            Module._MODULE_STACK.append(self)
        else:
            # Hack: keep non-empty module stack from constructor
            assert len(Module._MODULE_STACK) == 1 and Module._MODULE_STACK[0] is self
        self.device.__enter__()
        self.name_scope.__enter__()
        return self

    def __exit__(self, etype, exception, traceback):
        self.name_scope.__exit__(etype, exception, traceback)
        self.device.__exit__(etype, exception, traceback)
        popped = Module._MODULE_STACK.pop()
        assert popped is self
        assert self.is_initialized is not None
        if not self.is_initialized:
            assert len(Module._MODULE_STACK) == 0

    def initialize(self):
        assert self.is_initialized is None
        self.is_initialized = False

        with self:

            if self.summarizer is not None:
                directory = self.summarizer['directory']
                if os.path.isdir(directory):
                    directories = sorted(
                        d for d in os.listdir(directory)
                        if os.path.isdir(os.path.join(directory, d)) and d.startswith('summary-')
                    )
                else:
                    os.makedirs(directory)
                    directories = list()

                max_summaries = self.summarizer.get('max_summaries', 5)
                if len(directories) > max_summaries - 1:
                    for subdir in directories[:len(directories) - max_summaries + 1]:
                        subdir = os.path.join(directory, subdir)
                        os.remove(os.path.join(subdir, os.listdir(subdir)[0]))
                        os.rmdir(subdir)

                logdir = os.path.join(directory, time.strftime('summary-%Y%m%d-%H%M%S'))
                flush_millis = (self.summarizer.get('flush', 10) * 1000)
                # with tf.name_scope(name='summarizer'):
                self.summarizer = tf.summary.create_file_writer(
                    logdir=logdir, max_queue=None, flush_millis=flush_millis, filename_suffix=None,
                    name='summarizer'
                )

                # TODO: write agent spec?
                # tf.summary.text(name, data, step=None, description=None)

            super().initialize()

            self.core_initialize()

            # Units, used in: Parameter, Model.save(), Model.summarizer????
            self.units = dict(
                timesteps=self.timesteps, episodes=self.episodes, updates=self.updates
            )

            # Checkpoint manager
            if self.saver is not None:
                self.saver_directory = self.saver['directory']
                self.saver_filename = self.saver.get('filename', self.name)
                load = self.saver.get('load', False)
                # with tf.name_scope(name='saver'):
                self.checkpoint = tf.train.Checkpoint(**{self.name: self})
                self.saver = tf.train.CheckpointManager(
                    checkpoint=self.checkpoint, directory=self.saver_directory,
                    max_to_keep=self.saver.get('max_checkpoints', 5),
                    keep_checkpoint_every_n_hours=self.saver.get('max_hour_frequency'),
                    checkpoint_name=self.saver_filename,
                    step_counter=self.units[self.saver.get('unit', 'updates')],
                    checkpoint_interval=self.saver['frequency'], init_fn=None
                )

        self.is_initialized = True

        if self.summarizer is None:
            self.initialize_api()
        else:
            with self.summarizer.as_default():
                self.initialize_api()

        if self.saver is not None:
            if load:
                self.restore()
            else:
                self.save()

    def core_initialize(self):
        # Timestep counter
        self.timesteps = self.variable(
            name='timesteps', spec=TensorSpec(type='int'), initializer='zeros', is_trainable=False,
            is_saved=True
        )

        # Episode counter
        self.episodes = self.variable(
            name='episodes', spec=TensorSpec(type='int'), initializer='zeros', is_trainable=False,
            is_saved=True
        )

        # Update counter
        self.updates = self.variable(
            name='updates', spec=TensorSpec(type='int'), initializer='zeros', is_trainable=False,
            is_saved=True
        )

        # Episode reward
        self.episode_reward = self.variable(
            name='episode-reward',
            spec=TensorSpec(type=self.reward_spec.type, shape=(self.parallel_interactions,)),
            initializer='zeros', is_trainable=False, is_saved=False
        )

        # Internals buffers
        def function(name, spec, initial):
            shape = (self.parallel_interactions,) + spec.shape
            reps = (self.parallel_interactions,) + tuple(1 for _ in range(spec.rank))
            initializer = np.tile(np.expand_dims(initial, axis=0), reps=reps)
            return self.variable(
                name=(name.replace('/', '_') + '-buffer'),
                spec=TensorSpec(type=spec.type, shape=shape), initializer=initializer,
                is_trainable=False, is_saved=False
            )

        self.previous_internals = self.internals_spec.fmap(
            function=function, cls=VariableDict, with_names=True, zip_values=self.internals_init
        )

    def initialize_api(self):
        if self.summary_labels == 'all' or 'graph' in self.summary_labels:
            tf.summary.trace_on(graph=True, profiler=False)
        self.act(
            states=self.states_spec.empty(batched=True),
            auxiliaries=self.auxiliaries_spec.empty(batched=True),
            parallel=self.parallel_spec.empty(batched=True)
        )
        if self.summary_labels == 'all' or 'graph' in self.summary_labels:
            tf.summary.trace_export(name='act', step=self.timesteps, profiler_outdir=None)
            tf.summary.trace_on(graph=True, profiler=False)
        kwargs = dict(states=self.states_spec.empty(batched=True))
        if len(self.internals_spec) > 0:
            kwargs['internals'] = self.internals_spec.empty(batched=True)
        if len(self.auxiliaries_spec) > 0:
            kwargs['auxiliaries'] = self.auxiliaries_spec.empty(batched=True)
        self.independent_act(**kwargs)
        if self.summary_labels == 'all' or 'graph' in self.summary_labels:
            tf.summary.trace_export(
                name='independent-act', step=self.timesteps, profiler_outdir=None
            )
            tf.summary.trace_on(graph=True, profiler=False)
        self.observe(
            terminal=self.terminal_spec.empty(batched=True),
            reward=self.reward_spec.empty(batched=True),
            parallel=self.parallel_spec.empty(batched=False)
        )
        if self.summary_labels == 'all' or 'graph' in self.summary_labels:
            tf.summary.trace_export(name='observe', step=self.timesteps, profiler_outdir=None)

    def input_signature(self, *, function):
        if function == 'act':
            return SignatureDict(
                states=self.states_spec.signature(batched=True),
                auxiliaries=self.auxiliaries_spec.signature(batched=True),
                parallel=self.parallel_spec.signature(batched=True)
            )

        elif function == 'core_act':
            return SignatureDict(
                states=self.states_spec.signature(batched=True),
                internals=self.internals_spec.signature(batched=True),
                auxiliaries=self.auxiliaries_spec.signature(batched=True),
                parallel=self.parallel_spec.signature(batched=True)
            )

        elif function == 'core_observe':
            return SignatureDict(
                terminal=self.terminal_spec.signature(batched=True),
                reward=self.reward_spec.signature(batched=True),
                parallel=self.parallel_spec.signature(batched=False)
            )

        elif function == 'independent_act':
            signature = SignatureDict(states=self.states_spec.signature(batched=True))
            if len(self.internals_spec) > 0:
                signature['internals'] = self.internals_spec.signature(batched=True)
            if len(self.auxiliaries_spec) > 0:
                signature['auxiliaries'] = self.auxiliaries_spec.signature(batched=True)
            return signature

        elif function == 'observe':
            return SignatureDict(
                terminal=self.terminal_spec.signature(batched=True),
                reward=self.reward_spec.signature(batched=True),
                parallel=self.parallel_spec.signature(batched=False)
            )

        elif function == 'reset':
            return SignatureDict()

        else:
            return super().input_signature(function=function)

    @tf_function(num_args=0)
    def reset(self):
        timestep = tf_util.identity(input=self.timesteps)
        episode = tf_util.identity(input=self.episodes)
        update = tf_util.identity(input=self.updates)
        return timestep, episode, update

    @tf_function(num_args=3, optional=2)
    def independent_act(self, *, states, internals=None, auxiliaries=None):
        if internals is None:
            assert len(self.internals_spec) == 0
            internals = TensorDict()
        if auxiliaries is None:
            assert len(self.auxiliaries_spec) == 0
            auxiliaries = TensorDict()
        true = tf_util.constant(value=True, dtype='bool')
        batch_size = tf_util.cast(x=tf.shape(input=states.value())[0], dtype='int')

        # Input assertions
        assertions = list()
        if self.config.create_tf_assertions:
            assertions.extend(self.states_spec.tf_assert(
                x=states, batch_size=batch_size,
                message='Agent.independent_act: invalid {issue} for {name} state input.'
            ))
            assertions.extend(self.internals_spec.tf_assert(
                x=internals, batch_size=batch_size,
                message='Agent.independent_act: invalid {issue} for {name} internal input.'
            ))
            assertions.extend(self.auxiliaries_spec.tf_assert(
                x=auxiliaries, batch_size=batch_size,
                message='Agent.independent_act: invalid {issue} for {name} input.'
            ))
            # Mask assertions
            if self.config.enable_int_action_masking:
                for name, spec in self.actions_spec.items():
                    if spec.type == 'int':
                        assertions.append(tf.debugging.assert_equal(
                            x=tf.reduce_all(input_tensor=tf.math.reduce_any(
                                input_tensor=auxiliaries[name]['mask'], axis=(spec.rank + 1)
                            )), y=true,
                            message="Agent.independent_act: at least one action has to be valid."
                        ))

        with tf.control_dependencies(control_inputs=assertions):
            # Core act
            parallel = tf_util.zeros(shape=(1,), dtype='int')
            actions, internals = self.core_act(
                states=states, internals=internals, auxiliaries=auxiliaries, parallel=parallel,
                independent=True
            )
            # Skip action assertions

            # SavedModel requires flattened output
            if len(self.internals_spec) > 0:
                return OrderedDict(TensorDict(actions=actions, internals=internals))
            else:
                return OrderedDict(actions)

    @tf_function(num_args=3)
    def act(self, *, states, auxiliaries, parallel):
        true = tf_util.constant(value=True, dtype='bool')
        batch_size = tf_util.cast(x=tf.shape(input=parallel)[0], dtype='int')

        # Input assertions
        assertions = list()
        if self.config.create_tf_assertions:
            assertions.extend(self.states_spec.tf_assert(
                x=states, batch_size=batch_size,
                message='Agent.act: invalid {issue} for {name} state input.'
            ))
            assertions.extend(self.auxiliaries_spec.tf_assert(
                x=auxiliaries, batch_size=batch_size,
                message='Agent.act: invalid {issue} for {name} input.'
            ))
            assertions.extend(self.parallel_spec.tf_assert(
                x=parallel, batch_size=batch_size,
                message='Agent.act: invalid {issue} for parallel input.'
            ))
            # Mask assertions
            if self.config.enable_int_action_masking:
                for name, spec in self.actions_spec.items():
                    if spec.type == 'int':
                        assertions.append(tf.debugging.assert_equal(
                            x=tf.reduce_all(input_tensor=tf.math.reduce_any(
                                input_tensor=auxiliaries[name]['mask'], axis=(spec.rank + 1)
                            )), y=true,
                            message="Agent.independent_act: at least one action has to be valid."
                        ))

        with tf.control_dependencies(control_inputs=assertions):
            # Retrieve internals
            internals = self.previous_internals.fmap(
                function=(lambda x: tf.gather(params=x, indices=parallel)), cls=TensorDict
            )

            # Core act
            actions, internals = self.core_act(
                states=states, internals=internals, auxiliaries=auxiliaries, parallel=parallel,
                independent=False
            )

        # Action assertions
        assertions = list()
        if self.config.create_tf_assertions:
            assertions.extend(self.actions_spec.tf_assert(x=actions, batch_size=batch_size))
            if self.config.enable_int_action_masking:
                for name, spec, action in self.actions_spec.zip_items(actions):
                    if spec.type == 'int':
                        is_valid = tf.reduce_all(input_tensor=tf.gather(
                            params=auxiliaries[name]['mask'],
                            indices=tf.expand_dims(input=action, axis=(spec.rank + 1)),
                            batch_dims=(spec.rank + 1)
                        ))
                        assertions.append(tf.debugging.assert_equal(
                            x=is_valid, y=true, message="Action mask check."
                        ))

        # Remember internals
        dependencies = list()
        for name, previous, internal in self.previous_internals.zip_items(internals):
            sparse_delta = tf.IndexedSlices(values=internal, indices=parallel)
            dependencies.append(previous.scatter_update(sparse_delta=sparse_delta))

        # Increment timestep (after core act)
        with tf.control_dependencies(control_inputs=(actions.flatten() + internals.flatten())):
            dependencies.append(self.timesteps.assign_add(delta=batch_size, read_value=False))

        with tf.control_dependencies(control_inputs=(dependencies + assertions)):
            actions = actions.fmap(function=tf_util.identity)
            timestep = tf_util.identity(input=self.timesteps)
            return actions, timestep

    @tf_function(num_args=3)
    def observe(self, *, terminal, reward, parallel):
        zero = tf_util.constant(value=0, dtype='int')
        one = tf_util.constant(value=1, dtype='int')
        batch_size = tf_util.cast(x=tf.shape(input=terminal)[0], dtype='int')
        is_terminal = tf.concat(values=([zero], terminal), axis=0)[-1] > zero

        # Input assertions
        assertions = list()
        if self.config.create_tf_assertions:
            assertions.extend(self.terminal_spec.tf_assert(
                x=terminal, batch_size=batch_size,
                message='Agent.observe: invalid {issue} for terminal input.'
            ))
            assertions.extend(self.reward_spec.tf_assert(
                x=reward, batch_size=batch_size,
                message='Agent.observe: invalid {issue} for terminal input.'
            ))
            assertions.extend(self.parallel_spec.tf_assert(
                x=parallel, message='Agent.observe: invalid {issue} for parallel input.'
            ))
            # Assertion: at most one terminal
            assertions.append(tf.debugging.assert_less_equal(
                x=tf_util.cast(x=tf.math.count_nonzero(input=terminal), dtype='int'), y=one,
                message="Agent.observe: input contains more than one terminal."
            ))
            # Assertion: if terminal, last timestep in batch
            assertions.append(tf.debugging.assert_equal(
                x=tf.math.reduce_any(input_tensor=tf.math.greater(x=terminal, y=zero)), y=is_terminal,
                message="Agent.observe: terminal is not the last input timestep."
            ))

        with tf.control_dependencies(control_inputs=assertions):
            dependencies = list()

            # Reward summary
            if self.summary_labels == 'all' or 'reward' in self.summary_labels:
                with self.summarizer.as_default():
                    x = tf.math.reduce_mean(input_tensor=reward)
                    tf.summary.scalar(name='reward', data=x, step=self.timesteps)

            # Update episode reward
            sum_reward = tf.math.reduce_sum(input_tensor=reward)
            sparse_delta = tf.IndexedSlices(values=sum_reward, indices=parallel)
            dependencies.append(self.episode_reward.scatter_add(sparse_delta=sparse_delta))

            # Core observe (before terminal handling)
            updated = self.core_observe(terminal=terminal, reward=reward, parallel=parallel)
            dependencies.append(updated)

        # Handle terminal (after core observe and episode reward)
        with tf.control_dependencies(control_inputs=dependencies):

            def fn_terminal():
                operations = list()

                # Reset internals
                def function(spec, initial):
                    return tf_util.constant(value=initial, dtype=spec.type)

                initials = self.internals_spec.fmap(
                    function=function, cls=TensorDict, zip_values=self.internals_init
                )
                for name, previous, initial in self.previous_internals.zip_items(initials):
                    sparse_delta = tf.IndexedSlices(values=initial, indices=parallel)
                    operations.append(previous.scatter_update(sparse_delta=sparse_delta))

                # Episode reward summaries (before episode reward reset / episodes increment)
                if self.summary_labels == 'all' or 'reward' in self.summary_labels:
                    with self.summarizer.as_default():
                        x = tf.gather(params=self.episode_reward, indices=parallel)
                        tf.summary.scalar(name='episode-reward', data=x, step=self.episodes)

                # Reset episode reward
                zero_float = tf_util.constant(value=0.0, dtype='float')
                sparse_delta = tf.IndexedSlices(values=zero_float, indices=parallel)
                operations.append(self.episode_reward.scatter_update(sparse_delta=sparse_delta))

                # Increment episodes counter
                operations.append(self.episodes.assign_add(delta=one, read_value=False))

                return tf.group(*operations)

            handle_terminal = tf.cond(pred=is_terminal, true_fn=fn_terminal, false_fn=tf.no_op)

        with tf.control_dependencies(control_inputs=(handle_terminal,)):
            episodes = tf_util.identity(input=self.episodes)
            updates = tf_util.identity(input=self.updates)
            return updated, episodes, updates

    @tf_function(num_args=4)
    def core_act(self, *, states, internals, auxiliaries, parallel, independent):
        raise NotImplementedError

    @tf_function(num_args=3)
    def core_observe(self, *, terminal, reward, parallel):
        return tf_util.constant(value=False, dtype='bool')

    def get_variable(self, *, variable):
        assert False, 'Not updated yet!'
        if not variable.startswith(self.name):
            variable = util.join_scopes(self.name, variable)
        fetches = variable + '-output:0'
        return self.monitored_session.run(fetches=fetches)

    def assign_variable(self, *, variable, value):
        if variable.startswith(self.name + '/'):
            variable = variable[len(self.name) + 1:]
        module = self
        scope = variable.split('/')
        for _ in range(len(scope) - 1):
            module = module.modules[scope.pop(0)]
        fetches = util.join_scopes(self.name, variable) + '-assign'
        dtype = util.dtype(x=module.variables[scope[0]])
        feed_dict = {util.join_scopes(self.name, 'assignment-') + dtype + '-input:0': value}
        self.monitored_session.run(fetches=fetches, feed_dict=feed_dict)

    def summarize(self, *, summary, value, step=None):
        fetches = util.join_scopes(self.name, summary, 'write_summary', 'Const:0')
        feed_dict = {util.join_scopes(self.name, 'summarize-input:0'): value}
        if step is not None:
            feed_dict[util.join_scopes(self.name, 'summarize-step-input:0')] = step
        self.monitored_session.run(fetches=fetches, feed_dict=feed_dict)


        # if self.summarizer_spec is not None:
        #     if len(self.summarizer_spec.get('custom', ())) > 0:
        #         self.summarize_input = self.add_placeholder(
        #             name='summarize', dtype='float', shape=None, batched=False
        #         )
        #         # self.summarize_step_input = self.add_placeholder(
        #         #     name='summarize-step', dtype='int', shape=(), batched=False,
        #         #     default=self.timesteps
        #         # )
        #         self.summarize_step_input = self.timesteps
        #         self.custom_summaries = OrderedDict()
        #         for name, summary in self.summarizer_spec['custom'].items():
        #             if summary['type'] == 'audio':
        #                 self.custom_summaries[name] = tf.summary.audio(
        #                     name=name, data=self.summarize_input,
        #                     sample_rate=summary['sample_rate'],
        #                     step=self.summarize_step_input,
        #                     max_outputs=summary.get('max_outputs', 3),
        #                     encoding=summary.get('encoding')
        #                 )
        #             elif summary['type'] == 'histogram':
        #                 self.custom_summaries[name] = tf.summary.histogram(
        #                     name=name, data=self.summarize_input,
        #                     step=self.summarize_step_input,
        #                     buckets=summary.get('buckets')
        #                 )
        #             elif summary['type'] == 'image':
        #                 self.custom_summaries[name] = tf.summary.image(
        #                     name=name, data=self.summarize_input,
        #                     step=self.summarize_step_input,
        #                     max_outputs=summary.get('max_outputs', 3)
        #                 )
        #             elif summary['type'] == 'scalar':
        #                 self.custom_summaries[name] = tf.summary.scalar(
        #                     name=name,
        #                     data=tf.reshape(tensor=self.summarize_input, shape=()),
        #                     step=self.summarize_step_input
        #                 )
        #             else:
        #                 raise TensorforceError.value(
        #                     name='custom summary', argument='type', value=summary['type'],
        #                     hint='not in {audio,histogram,image,scalar}'
        #                 )

    def save(self, *, directory=None, filename=None, format='checkpoint', append=None):
        if directory is None and filename is None and format == 'checkpoint':
            if self.saver is None:
                raise TensorforceError.required(name='Model.save', argument='directory')
            if append is None:
                append = self.saver._step_counter
            else:
                append = self.units[append]
            return self.saver.save(checkpoint_number=append)

        if directory is None:
            raise TensorforceError.required(name='Model.save', argument='directory')

        if append is not None:
            append_value = self.units[append].numpy().item()

        if filename is None:
            filename = self.name

        if append is not None:
            filename = filename + '-' + str(append_value)

        if format == 'saved-model':
            directory = os.path.join(directory, filename)
            assert hasattr(self, '_independent_act_graphs')
            assert len(self._independent_act_graphs) == 1
            independent_act = next(iter(self._independent_act_graphs.values()))
            return tf.saved_model.save(obj=self, export_dir=directory, signatures=independent_act)

        if format == 'checkpoint':
            # which variables are not saved? should all be saved probably, so remove option
            # always write temporary terminal=2/3 to indicate it is in process... has been removed recently...
            # check everywhere temrinal is checked that this is correct, if 3 is used.
            # Reset should reset estimator!!!
            if self.checkpoint is None:
                self.checkpoint = tf.train.Checkpoint(**{self.name: self})

            # We are using the high-level "save" method of the checkpoint to write a "checkpoint" file.
            # This makes it easily restorable later on.
            # The base class uses the lower level "write" method, which doesn't provide such niceties.
            return self.checkpoint.save(file_prefix=os.path.join(directory, filename))

        # elif format == 'tensorflow':
        #     if self.summarizer_spec is not None:
        #         self.monitored_session.run(fetches=self.summarizer_flush)
        #     saver_path = self.saver.save(
        #         sess=self.session, save_path=path, global_step=append,
        #         # latest_filename=None,  # Defaults to 'checkpoint'.
        #         meta_graph_suffix='meta', write_meta_graph=True, write_state=True
        #     )
        #     assert saver_path.startswith(path)
        #     path = saver_path

        #     if not no_act_pb:
        #         graph_def = self.graph.as_graph_def()

        #         # freeze_graph clear_devices option
        #         for node in graph_def.node:
        #             node.device = ''

        #         graph_def = tf.compat.v1.graph_util.remove_training_nodes(input_graph=graph_def)
        #         output_node_names = [
        #             self.name + '.independent_act/' + name + '-output'
        #             for name in self.output_tensors['independent_act']
        #         ]
        #         # implies tf.compat.v1.graph_util.extract_sub_graph
        #         graph_def = tf.compat.v1.graph_util.convert_variables_to_constants(
        #             sess=self.monitored_session, input_graph_def=graph_def,
        #             output_node_names=output_node_names
        #         )
        #         graph_path = tf.io.write_graph(
        #             graph_or_graph_def=graph_def, logdir=directory,
        #             name=(os.path.split(path)[1] + '.pb'), as_text=False
        #         )
        #         assert graph_path == path + '.pb'
        #     return path

        elif format == 'numpy':
            variables = dict()
            for variable in self.saved_variables:
                variables[variable.name[len(self.name) + 1: -2]] = variable.numpy()
            path = os.path.join(directory, filename) + '.npz'
            np.savez(file=path, **variables)
            return path

        elif format == 'hdf5':
            path = os.path.join(directory, filename) + '.hdf5'
            with h5py.File(name=path, mode='w') as filehandle:
                for variable in self.saved_variables:
                    name = variable.name[len(self.name) + 1: -2]
                    filehandle.create_dataset(name=name, data=variable.numpy())
            return path

        else:
            raise TensorforceError.value(name='Model.save', argument='format', value=format)

    def restore(self, *, directory=None, filename=None, format='checkpoint'):
        if format == 'checkpoint':
            if directory is None:
                if self.saver is None:
                    raise TensorforceError.required(name='Model.save', argument='directory')
                directory = self.saver_directory
            if filename is None:
                filename = tf.train.latest_checkpoint(checkpoint_dir=directory)
                _directory, filename = os.path.split(filename)
                assert _directory == directory
            super().restore(directory=directory, filename=filename)

        elif format == 'saved-model':
            # TODO: Check memory/estimator/etc variables are not included!
            raise TensorforceError.value(name='Model.load', argument='format', value=format)

        # elif format == 'tensorflow':
        #     self.saver.restore(sess=self.session, save_path=path)

        elif format == 'numpy':
            if directory is None:
                raise TensorforceError(
                    name='Model.load', argument='directory', condition='format is "numpy"'
                )
            if filename is None:
                raise TensorforceError(
                    name='Model.load', argument='filename', condition='format is "numpy"'
                )
            variables = np.load(file=(os.path.join(directory, filename) + '.npz'))
            for variable in self.saved_variables:
                variable.assign(value=variables[variable.name[len(self.name) + 1: -2]])

        elif format == 'hdf5':
            if directory is None:
                raise TensorforceError(
                    name='Model.load', argument='directory', condition='format is "hdf5"'
                )
            if filename is None:
                raise TensorforceError(
                    name='Model.load', argument='filename', condition='format is "hdf5"'
                )
            path = os.path.join(directory, filename)
            if os.path.isfile(path + '.hdf5'):
                path = path + '.hdf5'
            else:
                path = path + '.h5'
            with h5py.File(name=path, mode='r') as filehandle:
                for variable in self.saved_variables:
                    variable.assign(value=filehandle[variable.name[len(self.name) + 1: -2]])

        else:
            raise TensorforceError.value(name='Model.load', argument='format', value=format)

        timesteps, episodes, updates = self.reset()
        return timesteps.numpy().item(), episodes.numpy().item(), updates.numpy().item()
