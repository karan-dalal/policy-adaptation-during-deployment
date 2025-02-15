import numpy as np
import torch
import os
from copy import deepcopy
from tqdm import tqdm
import utils
from video import VideoRecorder

from arguments import parse_args
from env.wrappers import make_pad_env
from agent.agent import make_agent
from utils import get_curl_pos_neg


def evaluate(env, agent, args, video, adapt=False):
	"""Evaluate an agent, optionally adapt using PAD"""
	episode_rewards = []

	for i in tqdm(range(args.pad_num_episodes)):
		ep_agent = deepcopy(agent) # make a new copy

		if args.use_curl: # initialize replay buffer for CURL
			replay_buffer = utils.ReplayBuffer(
				obs_shape=env.observation_space.shape,
				action_shape=env.action_space.shape,
				capacity=args.train_steps,
				batch_size=args.pad_batch_size
			)
		video.init(enabled=True)
		obs = env.reset()
		done = False
		episode_reward = 0
		losses = []
		step = 0
		ep_agent.train()
		frames = []
		actions = []
		stack = args.ss_stack
				
		# Add initial observations to frames list
		for i in range(3):
			frames.append(obs[3*i:3*i+3, :, :])

		while not done:
			# Take step
			with utils.eval_mode(ep_agent):
				action = ep_agent.select_action(obs)
			next_obs, reward, done, _ = env.step(action)
			episode_reward += reward

			# Add the most recent observation to the frames list	
			frames.append(next_obs[6:9, :, :])

			if len(frames) > stack + 1:
				frames.pop(0)
			
			# Set observations
			obs_extra = np.concatenate(frames[:-1], axis=0)
			next_obs_extra = np.concatenate(frames[1:], axis=0)

			# Make self-supervised update if flag is true
			if adapt:
				if args.use_rot: # rotation prediction

					# Prepare batch of cropped observations
					batch_next_obs = utils.batch_from_obs(torch.Tensor(next_obs).cuda(), batch_size=args.pad_batch_size)
					batch_next_obs = utils.random_crop(batch_next_obs)

					# Adapt using rotation prediction
					losses.append(ep_agent.update_rot(batch_next_obs))
				
				if args.use_inv: # inverse dynamics model

					# Prepare batch of observations
					batch_obs = utils.batch_from_obs(torch.Tensor(obs_extra).cuda(), batch_size=args.pad_batch_size)
					batch_next_obs = utils.batch_from_obs(torch.Tensor(next_obs_extra).cuda(), batch_size=args.pad_batch_size)
					batch_action = torch.Tensor(action).cuda().unsqueeze(0).repeat(args.pad_batch_size, 1)
					
					# Add action to actions list, remove, and concatenate
					actions.append(batch_action)
					if len(actions) > stack - 2:
						actions.pop(0)
					batch_action = torch.cat(actions, dim=1)

					# print("Before Cut", batch_obs.shape, batch_next_obs.shape, batch_action.shape)
					# print("--------The above sizes can be anything, multiples of 3----------")
					
					# Adapt using inverse dynamics prediction
					losses.append(ep_agent.update_inv(utils.random_crop(batch_obs), utils.random_crop(batch_next_obs), batch_action))

				if args.use_curl: # CURL

					# Add observation to replay buffer for use as negative samples
					# (only first argument obs is used, but we store all for convenience)
					replay_buffer.add(obs, action, reward, next_obs, True)

					# Prepare positive and negative samples
					obs_anchor, obs_pos = get_curl_pos_neg(next_obs, replay_buffer)

					# Adapt using CURL
					losses.append(ep_agent.update_curl(obs_anchor, obs_pos, ema=True))

			video.record(env, losses)
			obs = next_obs
			step += 1

		video.save(f'{args.mode}_pad_{i}.mp4' if adapt else f'{args.mode}_eval_{i}.mp4')
		episode_rewards.append(episode_reward)

	return np.mean(episode_rewards)


def init_env(args, setSeed):
		utils.set_seed_everywhere(setSeed)
		return make_pad_env(
			domain_name=args.domain_name,
			task_name=args.task_name,
			seed=setSeed,
			episode_length=args.episode_length,
			action_repeat=args.action_repeat,
			mode=args.mode
		)


def main(args):
	for setSeed in range(args.seed):
		# Initialize environment
		env = init_env(args, setSeed)
		#model_dir = utils.make_dir(os.path.join(args.work_dir, 'model'))
		model_dir = utils.make_dir(os.path.join("logs/cartpole_swingup/inv/0", 'model'))
		
		video_dir = utils.make_dir(os.path.join(args.work_dir, 'video'))
		video = VideoRecorder(video_dir if args.save_video else None, height=448, width=448)

		# Prepare agent
		assert torch.cuda.is_available(), 'must have cuda enabled'
		cropped_obs_shape = (3*args.frame_stack, 84, 84)
		agent = make_agent(
			obs_shape=cropped_obs_shape,
			action_shape=env.action_space.shape,
			args=args
		)
		agent.load(model_dir, args.pad_checkpoint)

		# Evaluate agent without PAD
		# print(f'Evaluating {args.work_dir} for {args.pad_num_episodes} episodes (mode: {args.mode})')
		# eval_reward = evaluate(env, agent, args, video)
		# print('eval reward:', int(eval_reward))

		# Evaluate agent with PAD (if applicable)
		pad_reward = None
		if args.use_inv or args.use_curl or args.use_rot:
			env = init_env(args, setSeed)
			print(f'Policy Adaptation during Deployment of {args.work_dir} for {args.pad_num_episodes} episodes (mode: {args.mode})')
			pad_reward = evaluate(env, agent, args, video, adapt=True)
			print('pad reward:', int(pad_reward))

		# Save results
		results_fp = os.path.join(args.work_dir, str(setSeed),f'pad_{args.mode}.pt')
		torch.save({
			'args': args,
			# 'eval_reward': eval_reward,
			'pad_reward': pad_reward
		}, results_fp)
		print('Saved results to', results_fp)


if __name__ == '__main__':
	args = parse_args()
	main(args)
