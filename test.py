import copy
import json
import os
import random
import time as time

import gym                  # type: ignore
import pandas as pd         # type: ignore
import torch
import numpy as np

import pynvml               # type: ignore
import PPO_model
from env.load_data import nums_detec
from env.fjsp_env import FJSPEnv
from typing import Dict, Any, List
import os
# from torch.backends.cudnn import set_flags

def setup_seed(seed: int):
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    if torch.cuda.is_available():
        # set_flags(_deterministic=True)
        torch.use_deterministic_algorithms(True)
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
        torch.cuda.manual_seed_all(seed)

def main():
    # Load config and init objects
    with open("./config.json", 'r') as load_f:
        load_dict: Dict[str, Dict[str, Any]] = json.load(load_f)
    env_paras: Dict[str, Any] = load_dict["env_paras"]
    model_paras: Dict[str, Any] = load_dict["model_paras"]
    train_paras: Dict[str, Any] = load_dict["train_paras"]
    test_paras: Dict[str, Any] = load_dict["test_paras"]

    deterministic: bool = train_paras["deterministic"]
    if deterministic:
        setup_seed(3407)            # magic seed
    # PyTorch initialization
    # gpu_tracker = MemTracker()  # Used to monitor memory (of gpu)
    cuda_available = torch.cuda.is_available()
    device = torch.device("cuda:0" if cuda_available else "cpu")
    if cuda_available:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    if device.type=='cuda':
        torch.cuda.set_device(device)
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    else:
        torch.set_default_tensor_type('torch.FloatTensor')
    print("PyTorch device: ", device.type)
    torch.set_printoptions(precision=None, threshold=np.inf, edgeitems=None, linewidth=None, profile=None, sci_mode=False)

    env_paras["device"] = device
    model_paras["device"] = device
    env_test_paras = copy.deepcopy(env_paras)
    num_ins = test_paras["num_ins"]
    if test_paras["sample"]:
        env_test_paras["batch_size"] = test_paras["num_sample"]
    else:
        env_test_paras["batch_size"] = 1
    model_paras["actor_in_dim"] = model_paras["out_size_ma"] * 2 + model_paras["out_size_ope"] * 2
    model_paras["critic_in_dim"] = model_paras["out_size_ma"] + model_paras["out_size_ope"]

    data_path = "./data_test/{0}/".format(test_paras["data_path"])
    test_files = os.listdir(data_path)
    test_files.sort(key=lambda x: x[:-4])       # sort by file name (without the last 4 characters, i.e. the .fjs extension suffix)
    test_files = test_files[:num_ins]           # fetch the first `num_ins` files (if not enough provided, all files will be used)
    mod_files = os.listdir('./model/')[:]

    memories = PPO_model.Memory()
    model = PPO_model.PPO(model_paras, train_paras) # the `train_paras` is just for initializing the model structure
    rules: List[str] = test_paras["rules"]
    envs: List[FJSPEnv] = []  # Store multiple environments

    # Detect and add models to "rules"
    # ↓ originally rules contains some notification strings like "DRL"
    if "DRL" in rules:      # auto add all models
        for root, _, fs in os.walk('./model/'):            # root, ds, fs
            for f in fs:
                if f.endswith('.pt'):
                    rules.append(f)
    # ↓ then, the notification strings are removed
    if len(rules) != 1:
        if "DRL" in rules:
            rules.remove("DRL")

    # Generate data files and fill in the header
    str_time = time.strftime("%Y%m%d_%H%M%S", time.localtime(time.time()))
    save_path = './save/test_{0}'.format(str_time)
    os.makedirs(save_path)
    makespan_excel_path = '{0}/makespan_{1}.xlsx'.format(save_path, str_time)
    time_excel_path = '{0}/time_{1}.xlsx'.format(save_path, str_time)
    writer = pd.ExcelWriter(makespan_excel_path)   # Makespan data storage path
    writer_time = pd.ExcelWriter(time_excel_path)  # time data storage path
    file_name = [test_files[i] for i in range(num_ins)]
    data_file = pd.DataFrame(file_name, columns=["file_name"])
    data_file.to_excel(writer, sheet_name='Sheet1', index=False)            # write the filename column
    # writer.save()
    writer.close()          # the `close()` is actually synonym for `save()`, to make it more file-like
    data_file.to_excel(writer_time, sheet_name='Sheet1', index=False)       # write the filename column
    # writer_time.save()
    writer_time.close()     # the `close()` is actually synonym for `save()`, to make it more file-like

    # Rule-by-rule (model-by-model) testing
    start = time.time()
    for i_rules in range(len(rules)):
        print(f'>>> Rules No. {i_rules + 1} (starting from 1)')
        rule = rules[i_rules]
        # Load trained model
        if rule.endswith('.pt'):
            if device.type == 'cuda':
                model_CKPT = torch.load('./model/' + mod_files[i_rules])
            else:
                model_CKPT = torch.load('./model/' + mod_files[i_rules], map_location='cpu')
            print('\nloading checkpoint:', mod_files[i_rules])
            model.policy.load_state_dict(model_CKPT)
            model.policy_old.load_state_dict(model_CKPT)
        print('rule:', rule)

        # Schedule instance by instance
        step_time_last = time.time()
        makespans = []
        times = []
        for i_ins in range(num_ins):
            test_file = data_path + test_files[i_ins]
            with open(test_file) as file_object:
                line = file_object.readlines()
                ins_num_jobs, ins_num_mas, _ = nums_detec(line)
            env_test_paras["num_jobs"] = ins_num_jobs
            env_test_paras["num_mas"] = ins_num_mas

            # Environment object already exists
            if len(envs) == num_ins:
                env = envs[i_ins]
            # Create environment object
            else:
                # Clear the existing environment
                if cuda_available:
                    meminfo = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    if meminfo.used / meminfo.total > 0.7:
                        envs.clear()
                # DRL-S, each env contains multiple (=num_sample) copies of one instance
                if test_paras["sample"]:
                    env = gym.make('fjsp-v0', case=[test_file] * test_paras["num_sample"],
                                   env_paras=env_test_paras, data_source='file')
                # DRL-G, each env contains one instance
                else:
                    env = gym.make('fjsp-v0', case=[test_file], env_paras=env_test_paras, data_source='file')
                envs.append(copy.deepcopy(env))
                print("Create env[{0}]".format(i_ins))

            # Schedule an instance/environment
            # DRL-S
            if test_paras["sample"]:
                print("use sampling strategy for env[{0}]".format(i_ins))
                # env.reset()
                # The author already reset the environment in the `__init__` function; however, in latest version of gym,
                # the `reset` function is forced to be called after the environment is created
                makespan, time_re = schedule(env, model, memories, flag_sample=test_paras["sample"])
                makespans.append(torch.min(makespan))
                times.append(time_re)
            # DRL-G
            else:
                print("using greedy strategy for env[{0}]".format(i_ins))
                time_s = []
                makespan_s = []  # In fact, the results obtained by DRL-G do not change
                # env.reset()
                # The author already reset the environment in the `__init__` function; however, in latest version of gym,
                # the `reset` function is forced to be called after the environment is created
                for j in range(test_paras["num_average"]):
                    makespan, time_re = schedule(env, model, memories)
                    makespan_s.append(makespan)
                    time_s.append(time_re)
                    env.reset()
                makespans.append(torch.mean(torch.tensor(makespan_s)))
                times.append(torch.mean(torch.tensor(time_s)))
            print("finish env {0}".format(i_ins))
        print("rule_spend_time: ", time.time() - step_time_last)

        # Save makespan and time data to files
        data = pd.DataFrame(torch.tensor(makespans).t().tolist(), columns=[rule])
        data.to_excel(writer, sheet_name='Sheet1', index=False, startcol=i_rules + 1)
        # writer.save()             # the `close()` is actually synonym for `save()`, to make it more file-like
        writer.close()
        data = pd.DataFrame(torch.tensor(times).t().tolist(), columns=[rule])
        data.to_excel(writer_time, sheet_name='Sheet1', index=False, startcol=i_rules + 1)
        # writer_time.save()        # the `close()` is actually synonym for `save()`, to make it more file-like
        writer_time.close()

        print(f'makespan results written at {makespan_excel_path}')
        print(f'time results written at {time_excel_path}')

        for env in envs:
            env.reset()

    print("total_spend_time: ", time.time() - start)

def schedule(env: FJSPEnv, model: PPO_model.PPO, memories: PPO_model.Memory, flag_sample: bool = False):
    # Get state and completion signal
    state = env.state
    # dones = env.done_batch
    done = False  # Unfinished at the beginning
    last_time = time.time()
    i = 0
    # not torch.tensor(False) == False
    while not done:  # ~False = -1, happened to be okay; but ~True = -2, which is not okay; later it's replace by torch.Tensor, which is okay
        i += 1
        with torch.no_grad():
            actions = model.policy_old.act(state, memories, flag_sample=flag_sample, flag_train=False)  # `dones` removed
        state, _, dones = env.step(actions)  # environment transit; second return: rewards, not used
        done = dones.all()  # type: ignore
    spend_time = time.time() - last_time  # The time taken to solve this environment (instance)
    # print("spend_time: ", spend_time)

    # Verify the solution
    gantt_result = env.validate_gantt()[0]
    if not gantt_result:
        print("Scheduling Error!!!!!!")
    return copy.deepcopy(env.makespan_batch), spend_time


if __name__ == '__main__':
    main()