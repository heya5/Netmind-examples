from NetmindMixins.Netmind import  hmp, NetmindModel, NetmindOptimizer
#!/usr/bin/env python3

import time
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address
from typing import Optional

import requests
import torch
import torch.nn as nn
import wandb
from torch_optimizer import Lamb
from transformers import BertConfig, BertForMaskedLM, HfArgumentParser

import hivemind
from hivemind.optim.state_averager import TrainingStateAverager
from hivemind.utils.logging import get_logger, use_hivemind_log_handler
from hivemind.utils.networking import log_visible_maddrs

from model import get_model
from data import get_data
from optimizer import get_optimizer


import utils
from arguments import (
    BaseTrainingArguments,
    ModelTrainingArguments,
    AveragerArguments,
    DatasetArguments,
    OptimizerArguments,
)

use_hivemind_log_handler("in_root_logger")
logger = get_logger(__name__)


@dataclass
class TrainingMonitorArguments(BaseTrainingArguments):
    """
    Note: You might want to have several initial peers so that if one dies,
    new workers still can join the collaboration via alive initial peers' addresses.
    Specify initial_peers argument for that purpose
    """

    use_google_dns: bool = field(
        default=False,
        metadata={
            "help": "Use Google DNS to determine the public IP address of this machine (and add it to --announce_maddrs)"
        },
    )
    refresh_period: float = field(default=30, metadata={"help": "Period (in seconds) for fetching the keys from DHT"})
    wandb_project: Optional[str] = field(
        default=None, metadata={"help": "Name of Weights & Biases project to report the training progress to"}
    )
    save_checkpoint_step_interval: int = field(
        default=5, metadata={"help": "Frequency (in steps) of fetching and saving state from peers"}
    )
    model_config_path: str = field(
        default="https://s3.amazonaws.com/models.huggingface.co/bert/albert-large-v2-config.json",
        metadata={"help": "Path to the model config"},
    )
    repo_path: Optional[str] = field(
        default=None, metadata={"help": "Path to local repository to store the model and optimizer states"}
    )
    repo_url: Optional[str] = field(
        default=None, metadata={"help": "URL of Hugging Face Hub repository to upload the model and optimizer states"}
    )
    upload_interval: Optional[float] = field(
        default=None, metadata={"help": "Frequency (in seconds) of uploading the model to Hub"}
    )
    store_checkpoints: bool = field(default=False, metadata={"help": "If True, enables CheckpointHandler"})

class CheckpointHandler:
    def __init__(
        self,
        dataset_args: DatasetArguments,
        monitor_args: TrainingMonitorArguments,
        optimizer_args: OptimizerArguments,
        averager_args: AveragerArguments,
        dht: hivemind.DHT,
        training_args: ModelTrainingArguments,
    ):
        self.save_checkpoint_step_interval = monitor_args.save_checkpoint_step_interval
        self.repo_path = monitor_args.repo_path
        self.repo_url = monitor_args.repo_url
        self.upload_interval = monitor_args.upload_interval
        self.previous_step = -1

        
        self.model = get_model(training_args)
        self.model = NetmindModel(self.model)

        opt = self.get_optimizer(training_args)
        opt = NetmindOptimizer(opt)

        self.state_averager = TrainingStateAverager(
            dht=dht,
            optimizer=opt,
            prefix=experiment_prefix,
            state_compression=hivemind.Float16Compression(),
            bandwidth=optimizer_args.bandwidth,
            client_mode=optimizer_args.client_mode,
            start=True,
            **asdict(averager_args),
        )
        self.previous_timestamp = time.time()

    def is_time_to_save_state(self, cur_step):
        if self.save_checkpoint_step_interval is None:
            return False
        elif cur_step - self.previous_step >= self.save_checkpoint_step_interval:
            return True
        else:
            return False

    def save_state(self, cur_step):
        logger.info("Saving state from peers")
        self.state_averager.load_state_from_peers()
        self.previous_step = cur_step

    def is_time_to_upload(self):
        if time.time() - self.previous_timestamp >= self.upload_interval:
            return True
        else:
            return False

    def get_optimizer(self, training_args):
        opt = torch.optim.SGD(
            self.model.parameters(),
            lr=training_args.learning_rate,
            momentum=training_args.momentum,
            weight_decay=training_args.weight_decay,
        )
        return opt

    def upload_checkpoint(self, current_loss):
        hmp.save_pretrained()
        # Upload models to netmind
        self.previous_timestamp = time.time()

if __name__ == "__main__":
    parser = HfArgumentParser((DatasetArguments, TrainingMonitorArguments, OptimizerArguments, AveragerArguments,ModelTrainingArguments))
    dataset_args, monitor_args, optimizer_args, averager_args, training_args = parser.parse_args_into_dataclasses()

    if monitor_args.use_google_dns:
        request = requests.get("https://api.ipify.org")
        request.raise_for_status()

        address = request.text
        logger.info(f"Received public IP address of this machine: {address}")
        version = ip_address(address).version
        monitor_args.announce_maddrs += [f"/ip{version}/{address}/tcp/0"]

    experiment_prefix = monitor_args.experiment_prefix
    validators, local_public_key = utils.make_validators(experiment_prefix)

    dht = hivemind.DHT(
        start=True,
        initial_peers=monitor_args.initial_peers,
        record_validators=validators,
        use_ipfs=monitor_args.use_ipfs,
        host_maddrs=monitor_args.host_maddrs,
        announce_maddrs=monitor_args.announce_maddrs,
        identity_path=monitor_args.identity_path,
    )
    hmp.init(dht, local_public_key)
    log_visible_maddrs(dht.get_visible_maddrs(), only_p2p=monitor_args.use_ipfs)

    if monitor_args.wandb_project is not None:
        wandb.init(project=monitor_args.wandb_project)

    current_step = 0
    monitor_metrics = {}
    if monitor_args.store_checkpoints:
        checkpoint_handler = CheckpointHandler(dataset_args, monitor_args, optimizer_args, averager_args, dht, training_args)

    while True:
        metrics_dict = dht.get(experiment_prefix + "_metrics", latest=True)

        if metrics_dict is not None:
            metrics_dict = metrics_dict.value
            metrics = [utils.LocalMetrics.parse_obj(metrics_dict[peer].value) for peer in metrics_dict]
            latest_step = max(item.step for item in metrics)

            if latest_step != current_step:
                logger.debug(f"Got metrics from {len(metrics)} peers")

                for i, metrics_for_peer in enumerate(metrics):
                    logger.debug(f"{i} peer {metrics_for_peer}")

                current_step = latest_step
                alive_peers = 0
                sum_loss = 0
                num_samples = 0
                sum_perf = 0
                sum_mini_steps = 0

                for item in metrics:
                    sum_loss += item.loss
                    alive_peers += 1
                    sum_perf += item.samples_per_second
                    num_samples += item.samples_accumulated
                    sum_mini_steps += item.mini_steps
                current_loss = sum_loss / sum_mini_steps
                logger.info(f"Step #{current_step}\tloss = {current_loss:.5f}")

                monitor_metrics = {
                    "loss": current_loss,
                    "alive peers": alive_peers,
                    "samples": num_samples,
                    "performance": sum_perf
                }

                if monitor_args.store_checkpoints:
                    if checkpoint_handler.is_time_to_save_state(current_step):
                        checkpoint_handler.save_state(current_step)
                        if checkpoint_handler.is_time_to_upload():
                            checkpoint_handler.upload_checkpoint(current_loss)
        logger.debug("Peer is still alive...")
        time.sleep(monitor_args.refresh_period)
        hmp.step(current_step, monitor_metrics)
