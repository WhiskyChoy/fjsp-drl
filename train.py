import copy
import json
import os
import random
import time
from collections import deque

import gym                  # type: ignore
import pandas as pd         # type: ignore
import torch
import numpy as np
from visdom import Visdom

import PPO_model
from env.case_generator import CaseGenerator
from validate import validate, get_validate_env
from typing import Dict, Any
from env.fjsp_env import FJSPEnv
import subprocess
# from torch.backends.cudnn import set_flags

def setup_seed(seed: int):
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    if torch.cuda.is_available():
        # set_flags(_deterministic=True)
        torch.use_deterministic_algorithms(True, warn_only=True)    # ` scatter_add_cuda_kernel` does not have a deterministic implementation, so we set `warn_only=True`
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
        torch.cuda.manual_seed_all(seed)

def main():
    # Load config and init objects
    with open("./config.json", 'r') as load_f:
        load_dict: Dict[str, Dict[str, Any]] = json.load(load_f)
    
    env_paras: Dict[str, Any] = load_dict["env_paras"]
    model_paras: Dict[str, Any] = load_dict["model_paras"]
    train_paras: Dict[str, Any] = load_dict["train_paras"]

    deterministic: bool = train_paras["deterministic"]
    if deterministic:
        setup_seed(3407)            # magic seed
    # PyTorch initialization
    # gpu_tracker = MemTracker()  # Used to monitor memory (of gpu)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if device.type == 'cuda':
        torch.cuda.set_device(device)
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    else:
        torch.set_default_tensor_type('torch.FloatTensor')
    print("PyTorch device: ", device.type)
    torch.set_printoptions(precision=None, threshold=np.inf, edgeitems=None, linewidth=None, profile=None, sci_mode=False)

    env_paras["device"] = device
    model_paras["device"] = device
    env_valid_paras = copy.deepcopy(env_paras)
    env_valid_paras["batch_size"] = env_paras["valid_batch_size"]
    model_paras["actor_in_dim"] = model_paras["out_size_ma"] * 2 + model_paras["out_size_ope"] * 2
    model_paras["critic_in_dim"] = model_paras["out_size_ma"] + model_paras["out_size_ope"]

    num_jobs: int = env_paras["num_jobs"]
    num_mas: int = env_paras["num_mas"]
    opes_per_job_min: int = int(num_mas * 0.8)
    opes_per_job_max: int = int(num_mas * 1.2)

    memories = PPO_model.Memory()
    model = PPO_model.PPO(model_paras, train_paras, num_envs=env_paras["batch_size"])
    env_valid = get_validate_env(env_valid_paras)   # Create an environment for validation
    maxlen = 1                                      # Save the best model (Top-maxlen models)
    best_models = deque()
    makespan_best = float('inf')

    # Use visdom to visualize the training process
    is_viz: bool = train_paras["viz"]
    if is_viz:
        # os.system('python -m visdom.server')
        # Importing required module
        # Using system() method to execute shell commands
        background_viz: bool = train_paras["background_viz"]
        if background_viz:
            subprocess.Popen('python -m visdom.server', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)      # subprocess.call is blocking
        instant_viz_browser: bool = train_paras["instant_viz_browser"]
        if instant_viz_browser:
            subprocess.Popen('python -m webbrowser http://localhost:8097', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # actually output to stderr↑
        viz = Visdom(env=train_paras["viz_name"])

    # Generate data files and fill in the header
    str_time = time.strftime("%Y%m%d_%H%M%S", time.localtime(time.time()))
    save_path = './save/train_{0}'.format(str_time)
    os.makedirs(save_path)
    # Training curve storage path (average of validation set)
    writer_ave = pd.ExcelWriter('{0}/training_ave_{1}.xlsx'.format(save_path, str_time))
    # Training curve storage path (value of each validating instance)
    writer_100 = pd.ExcelWriter('{0}/training_100_{1}.xlsx'.format(save_path, str_time))
    valid_results = []
    valid_results_100 = []
    data_file = pd.DataFrame(np.arange(10, 1010, 10), columns=["iterations"])
    data_file.to_excel(writer_ave, sheet_name='Sheet1', index=False)
    writer_ave.save()
    writer_ave.close()
    data_file = pd.DataFrame(np.arange(10, 1010, 10), columns=["iterations"])
    data_file.to_excel(writer_100, sheet_name='Sheet1', index=False)
    writer_100.save()
    writer_100.close()

    # Start training iteration
    start_time = time.time()
    env: FJSPEnv = None         # type: ignore
    for i in range(1, train_paras["max_iterations"]+1):
        # Replace training instances every x iteration (x = 20 in paper)
        if (i - 1) % train_paras["parallel_iter"] == 0:
            # \mathcal{B} instances use consistent operations to speed up training
            nums_ope = [random.randint(opes_per_job_min, opes_per_job_max) for _ in range(num_jobs)]
            case = CaseGenerator(num_jobs, num_mas, opes_per_job_min, opes_per_job_max, nums_ope=nums_ope)
            env = gym.make('fjsp-v0', case=case, env_paras=env_paras)
            print('num_job: ', num_jobs, '\tnum_mas: ', num_mas, '\tnum_opes: ', sum(nums_ope))

        # Get state and completion signal
        state = env.state
        done = False
        # dones = env.done_batch
        last_time = time.time()

        # Schedule in parallel
        while not done:    # ~False = -1, happened to be okay; but ~True = -2, which is not okay; later it's replace by torch.Tensor, which is okay
            with torch.no_grad():
                actions = model.policy_old.act(state, memories)      # the `memories` stores the data for training; `dones` removed
            state, rewards, dones = env.step(actions)
            done = dones.all()
            memories.rewards.append(rewards)
            memories.is_terminals.append(dones)
            # gpu_tracker.track()  # Used to monitor memory (of gpu)
        print("spend_time: ", time.time()-last_time)

        # Verify the solution
        gantt_result = env.validate_gantt()[0]
        if not gantt_result:
            print("Scheduling Error!!!!!!")
        # print("Scheduling Finish")
        env.reset()

        # if iter mod x = 0 then update the policy (x = 1 in paper)
        if i % train_paras["update_timestep"] == 0:
            loss, reward = model.update(memories, env_paras, train_paras)
            print("reward: ", '%.3f' % reward, "; loss: ", '%.3f' % loss)
            memories.clear_memory()
            if is_viz:
                viz.line(X=np.array([i]), Y=np.array([reward]),
                    win='window{}'.format(0), update='append', opts=dict(title='reward of envs'))
                viz.line(X=np.array([i]), Y=np.array([loss]),
                    win='window{}'.format(1), update='append', opts=dict(title='loss of envs'))  # deprecated

        # if iter mod x = 0 then validate the policy (x = 10 in the paper)
        if i % train_paras["save_timestep"] == 0:
            print('\nStart validating')
            # Record the average results and the results on each instance
            vali_result, vali_result_100 = validate(env_valid_paras, env_valid, model.policy_old)
            valid_results.append(vali_result.item())
            valid_results_100.append(vali_result_100)

            # Save the best model
            if vali_result < makespan_best:
                makespan_best = vali_result
                if len(best_models) == maxlen:
                    delete_file = best_models.popleft()
                    os.remove(delete_file)
                save_file = '{0}/save_best_{1}_{2}_{3}.pt'.format(save_path, num_jobs, num_mas, i)
                best_models.append(save_file)
                torch.save(model.policy.state_dict(), save_file)

            if is_viz:
                viz.line(
                    X=np.array([i]), Y=np.array([vali_result.item()]),
                    win='window{}'.format(2), update='append', opts=dict(title='makespan of valid'))

    # Save the data of training curve to files
    data = pd.DataFrame(np.array(valid_results).transpose(), columns=["res"])
    data.to_excel(writer_ave, sheet_name='Sheet1', index=False, startcol=1)
    # writer_ave.save()
    writer_ave.close()      # the `close()` is actually synonym for `save()`, to make it more file-like
    column = [i_col for i_col in range(100)]
    data = pd.DataFrame(np.array(torch.stack(valid_results_100, dim=0).to('cpu')), columns=column)
    data.to_excel(writer_100, sheet_name='Sheet1', index=False, startcol=1)
    # writer_100.save()
    writer_100.close()      # the `close()` is actually synonym for `save()`, to make it more file-like

    print("total_time: ", time.time() - start_time)

if __name__ == '__main__':
    main()