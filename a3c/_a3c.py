import tensorflow as tf
import numpy as np
import scipy.signal

from models import CNNPolicy, LinearPolicy
from tensorflow import losses
import matplotlib.pyplot as plt

def discount(x, gamma):
	return scipy.signal.lfilter([1], [1, -gamma], x[::-1], axis=0)[::-1]

def show_images(images, cols = 1, titles = None):
	"""Display a list of images in a single figure with matplotlib.
	
	Parameters
	---------
	images: List of np.arrays compatible with plt.imshow.
	
	cols (Default = 1): Number of columns in figure (number of rows is 
						set to np.ceil(n_images/float(cols))).
	
	titles: List of titles corresponding to each image. Must have
			the same length as titles.
	"""
	assert((titles is None) or (len(images) == len(titles)))
	n_images = len(images)
	if titles is None: titles = ['Image (%d)' % i for i in range(1,n_images + 1)]
	fig = plt.figure()
	for n, (image, title) in enumerate(zip(images, titles)):
		a = fig.add_subplot(cols, np.ceil(n_images/float(cols)), n + 1)
		if image.ndim == 2:
			plt.gray()
		plt.imshow(image)
		a.set_title(title)
	fig.set_size_inches(np.array(fig.get_size_inches()) * n_images)
	plt.show()

class ReplayBuffer():
	def __init__(self, state_dims):
		self.empty()
		self.state_dims = state_dims

	def add_transition(self, state, action, reward, done, next_state, value):
		self.size += 1
		self.states.append(state)
		self.actions.append(action)
		self.rewards.append(reward)
		self.dones.append(done)
		self.next_states.append(next_state)
		self.values.append(value)

	def empty(self):
		self.states = []
		self.actions = []
		self.rewards = []
		self.dones = []
		self.next_states = []
		self.values = []
		self.size = 0

class Actor():
	def __init__(self, config, sess, scope):
		self.config = config
		self.sess = sess
		self.num_actions = config.output_dims
		if config.policy_type == 'cnn':
			self.policy = CNNPolicy(config, scope)
		elif config.policy_type == 'linear':
			self.policy = LinearPolicy(config, scope)

	def act(self, state):
		actions, values, logits, pi = self.sess.run([
			self.policy.actions, self.policy.vf, self.policy.logits, self.policy.pi],
			feed_dict={
				self.policy.inputs: state
			})
		# action_dist = action_dist[0]
		# actions = np.random.choice(list(range(self.num_actions)), 
		# 	p=action_dist)
		#print(logits)
		return [actions], values

class Worker():
	def __init__(self, 
		env, 
		name,
		sess,
		output_path, config):

		self.env = env
		self.num_envs = env.num_envs
		self.name = name
		self.sess = sess
		self.config = config
		self.output_path = output_path

		self.total_iterations = 0
		self.episodes = 0

		worker_device = "/job:worker/task:{}/device:cpu:{}".format(config.task_index, config.task_index)
		#with tf.device(tf.train.replica_device_setter(1, worker_device=worker_device)):
		with tf.device("/job:ps/task:0/device:cpu:0"):
			#with tf.variable_scope("global"):
			self.global_actor = Actor(config, sess, 'global')
		with tf.device(worker_device):
			#with tf.variable_scope("local"):
			self.actor = Actor(config, sess, 'local')

		self._sync = tf.group(*[v1.assign(v2) for v1, v2 in zip(self.actor.policy.vars, self.global_actor.policy.vars)])
		self.replay_buffer = ReplayBuffer(config.input_dims)

		self.total_rewards = []

		self.a3c()
		self.add_summary()

	def process_state(self, frame, buff):
		frame = np.expand_dims(process_frame(frame), axis=0)
		buff = buff[1:] + [frame]
		state = np.concatenate(buff, axis=3)
		#show_images([np.squeeze(img) for img in buff])
		return state, buff

	def process_state_(self, frame):
		return np.expand_dims(frame, axis=0)

	def run(self):
		self.sync()
		state = self.env.reset().astype(np.float32) / 255.

		total_reward = np.zeros([self.num_envs])
		while True:
			self.total_iterations += 1
			action, value = self.actor.act(state)
			next_state, reward, done, info = self.env.step(action)

			next_state = next_state.astype(np.float32) / 255.

			# next_state, buff = self.process_state(next_frame, buff)
			# next_state = self.process_state_(next_frame)

			self.replay_buffer.add_transition(state, action, reward[-1], done, next_state, value)
			state = next_state

			#self.env.render()
			done = done[-1]

			total_reward += reward
			# if our buffer is full
			if self.replay_buffer.size >= self.config.batch_size:
				# process rollouts first to calculate discounted rewards
				bootstrap = None

				if not done:
					_, bootstrap = self.actor.act(state)

				self.train(bootstrap=bootstrap)

			if done:
				break
		# train after rollout!

		if self.replay_buffer.size > 0:
			self.train()

		self.episodes += 1
		#if self.episodes % 10 == 0:
		print('==============================')
		print('Loss/Entropy for episode {} is {}/{}'.format(self.episodes, self.report_loss, self.report_entropy))
		print('Reward for episode {} is {}'.format(self.episodes, total_reward))
		print('Now at iteration {}'.format(self.total_iterations))

		summary = tf.Summary()
		summary.value.add(tag='Episode Rewards', simple_value=np.mean(total_reward))
		self.file_writer.add_summary(summary, self.total_iterations)
		self.file_writer.flush()

		return total_reward

	def train(self, bootstrap=None):
		target_v, adv = self.process_rollouts(bootstrap=bootstrap)

		buff = self.replay_buffer
		_, summary, entropy, loss, logits, v = self.sess.run([
			self.train_op, self.merged, self.entropy, self.loss, self.actor.policy.logits, self.target_v],
			feed_dict={
				self.action: np.reshape(buff.actions, (-1)),
				self.adv: adv,
				self.target_v: target_v,
				self.actor.policy.inputs: np.reshape(buff.states, (-1,) + tuple(buff.state_dims))
			})
		self.report_loss = loss
		self.report_entropy = entropy
		self.replay_buffer.empty()
		
		self.file_writer.add_summary(summary, self.total_iterations)
		self.file_writer.flush()

	def add_summary(self):
		tf.summary.scalar("Loss", self.loss)
		tf.summary.scalar("Entropy", self.entropy)
		self.merged = tf.summary.merge_all()
		self.file_writer = tf.summary.FileWriter(self.output_path, self.sess.graph)

	def a3c(self):
		self.action = tf.placeholder(tf.int32, shape=[None])
		self.adv = tf.placeholder(tf.float32, shape=[None])
		self.target_v = tf.placeholder(tf.float32, shape=[None])

		actor = self.actor
		# make sure we pass in logits
		log_prob = tf.nn.sparse_softmax_cross_entropy_with_logits(
			labels=self.action,
			logits=actor.policy.logits)

		policy_loss = tf.reduce_mean(self.adv * log_prob)
		value_loss = tf.losses.mean_squared_error(self.target_v, actor.policy.vf)
		# make sure we pass in the probability distribution
		self.entropy = -tf.reduce_mean(tf.reduce_sum(tf.nn.softmax(actor.policy.logits) * \
			tf.nn.log_softmax(actor.policy.logits + 1e-7), axis=1))

		self.loss = policy_loss + self.config.vf_coeff * value_loss - \
			self.config.entropy_coeff * self.entropy

		# variables = sorted(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'local'), key=lambda x: x.name)
		# global_variables = sorted(tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global'), key=lambda x: x.name)
		gradients = tf.gradients(self.loss, actor.policy.vars)
		optimizer = tf.train.AdamOptimizer(learning_rate=self.config.lr)
		gradients, _ = tf.clip_by_global_norm(gradients, self.config.max_grad_norm)

		grads_and_vars = zip(gradients, self.global_actor.policy.vars)
		self.train_op = optimizer.apply_gradients(grads_and_vars)

	def process_rollouts(self, bootstrap=None):
		buff = self.replay_buffer
		
		reward_with_v = np.concatenate([buff.rewards, [0 if bootstrap == None else bootstrap]])
		value_with_v = np.concatenate([buff.values, [0 if bootstrap == None else bootstrap]])

		disc_reward = discount(reward_with_v, self.config.gamma)[:-1]

		temporal_diff = buff.rewards + self.config.gamma * value_with_v[1:] - value_with_v[:-1]

		advantage = discount(temporal_diff, self.config.gamma)

		return disc_reward, advantage

	def sync(self):
		self.sess.run([self._sync])

	def update(self):
		pass