from abc import abstractmethod

import torch
from numpy import inf
import torch.distributed as dist


class BaseTrainer:
    """
    Base class for all trainers
    """
    def __init__(self, model, loss, metrics, optimizer, config, writer=None, init_val=False, debug=False):
        self.config = config
        self.logger = config.get_logger('trainer', config['trainer']['verbosity'])

        self.ddp = False
        try:
            if config['strat'] == 'ddp':
                self.ddp = True
        except:
            self.logger.info("No strat set in config file, assuming not DDP")
            self.ddp = False # Not set in config file, for backwards compatability
        if self.ddp:
            self.global_rank = dist.get_rank()
            self.local_rank = self.global_rank % config['n_gpu']

        self.init_val = init_val
        # setup GPU device if available, move model into configured device
        self.device, device_ids = self._prepare_device(config['n_gpu'], debug)
        print(self.device)
        self.model = model.to(self.device)
        self.model.device = self.device
        
        
        if len(device_ids) > 1 and not self.ddp:
            self.model = torch.nn.DataParallel(model, device_ids=device_ids)
            self.logger.info("Using DataParallel!")
        elif self.ddp:
            self.model = torch.nn.parallel.DistributedDataParallel(model, 
                device_ids=[self.local_rank], output_device=[self.local_rank])
            self.logger.info("Using DistributedDataParallel!")

        loss = loss.to(self.device)
        self.loss = loss
        self.metrics = metrics
        self.optimizer = optimizer

        cfg_trainer = config['trainer']
        self.epochs = cfg_trainer['epochs']
        self.debugging = cfg_trainer.get('debugging', False)
        self.save_period = cfg_trainer['save_period']
        self.monitor = cfg_trainer.get('monitor', 'off')
        self.init_val = cfg_trainer.get('init_val', True)

        # configuration to monitor model performance and save best
        if self.monitor == 'off':
            self.mnt_mode = 'off'
            self.mnt_best = 0
        else:
            self.mnt_mode, self.mnt_metric = self.monitor.split()
            assert self.mnt_mode in ['min', 'max']

            self.mnt_best = inf if self.mnt_mode == 'min' else -inf
            self.early_stop = cfg_trainer.get('early_stop', inf)

        self.start_epoch = 1

        self.checkpoint_dir = config.save_dir

        # setup visualization writer instance                
        #self.writer = TensorboardWriter(config.log_dir, self.logger, cfg_trainer['tensorboard'])
        self.writer = writer

        if config.resume is not None:
            self._resume_checkpoint(config.resume)

    @abstractmethod
    def _train_epoch(self, epoch):
        """
        Training logic for an epoch

        :param epoch: Current epoch number
        """
        raise NotImplementedError

    @abstractmethod
    def _valid_epoch(self, epoch):
        """
        Training logic for an epoch

        :param epoch: Current epoch number
        """
        raise NotImplementedError


    def train(self):
        """
        Full training logic
        """
        not_improved_count = 0

        if self.init_val:
            _ = self._valid_epoch(-1)

        for epoch in range(self.start_epoch, self.epochs + 1):
            result = self._train_epoch(epoch)
            
            # save logged informations into log dict
            log = {'epoch': epoch}
            for key, value in result.items():
                if key == 'metrics':
                    log.update({mtr.__name__: value[i]
                                for i, mtr in enumerate(self.metrics)})
                elif key == 'val_metrics':
                    log.update({'val_' + mtr.__name__: value[i]
                                for i, mtr in enumerate(self.metrics)})
                elif key == 'nested_val_metrics':
                    # NOTE: currently only supports two layers of nesting
                    for subkey, subval in value.items():
                        for subsubkey, subsubval in subval.items():
                            for subsubsubkey, subsubsubval in subsubval.items():
                                log[f"val_{subkey}_{subsubkey}_{subsubsubkey}"] = subsubsubval
                else:
                    log[key] = value

            # print logged informations to the screen
            if self.ddp:
                if self.global_rank == 0:
                    for key, value in log.items():
                        self.logger.info('    {:15s}: {}'.format(str(key), value))
            else:
                for key, value in log.items():
                    self.logger.info('    {:15s}: {}'.format(str(key), value))

            # evaluate model performance according to configured metric, save best checkpoint as model_best
            best = False
            if self.mnt_mode != 'off':
                try:
                    # check whether model performance improved or not, according to specified metric(mnt_metric)
                    improved = (self.mnt_mode == 'min' and log[self.mnt_metric] <= self.mnt_best) or \
                               (self.mnt_mode == 'max' and log[self.mnt_metric] >= self.mnt_best)
                except KeyError:
                    self.logger.warning("Warning: Metric '{}' is not found. "
                                        "Model performance monitoring is disabled.".format(self.mnt_metric))
                    self.mnt_mode = 'off'
                    improved = False

                if improved:
                    self.mnt_best = log[self.mnt_metric]
                    not_improved_count = 0
                    best = True
                else:
                    not_improved_count += 1

                if not_improved_count > self.early_stop:
                    self.logger.info("Validation performance didn\'t improve for {} epochs. "
                                     "Training stops.".format(self.early_stop))
                    break

            if not self.debugging and (epoch % self.save_period == 0 or best):
                #import pdb; pdb.set_trace()
            #if best:
                if self.ddp:
                    if self.global_rank == 0:
                        self._save_checkpoint(epoch, save_best=best)
                else:
                    self._save_checkpoint(epoch, save_best=best)

    def _prepare_device(self, n_gpu_use, debug=False):
        """
        setup GPU device if available, move model into configured device
        """
        if debug:
            device = torch.device('cuda:{}'.format(0))
            list_ids = [0]
            self.logger.warning("Warning: Debugging mode, using only GPU 0 for all processes")
            return device, list_ids
        n_gpu = torch.cuda.device_count()
        if n_gpu_use > 0 and n_gpu == 0:
            self.logger.warning("Warning: There\'s no GPU available on this machine,"
                                "training will be performed on CPU.")
            n_gpu_use = 0
        if n_gpu_use > n_gpu:
            self.logger.warning("Warning: The number of GPU\'s configured to use is {}, but only {} are available "
                                "on this machine.".format(n_gpu_use, n_gpu))
            n_gpu_use = n_gpu
        device = torch.device('cuda:0' if n_gpu_use > 0 else 'cpu')
        list_ids = list(range(n_gpu_use))
        if self.ddp:
            device = torch.device('cuda:{}'.format(self.local_rank))
            list_ids = [self.local_rank]
        return device, list_ids

    def _save_checkpoint(self, epoch, save_best=False):
        """
        Saving checkpoints

        :param epoch: current epoch number
        :param log: logging information of the epoch
        :param save_best: if True, rename the saved checkpoint to 'model_best.pth'
        """
        arch = type(self.model).__name__
        state = {
            'arch': arch,
            'epoch': epoch,
            'state_dict': self.model.state_dict() if not self.ddp else self.model.module.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'monitor_best': self.mnt_best,
            'config': self.config
        }
        filename = str(self.checkpoint_dir / 'checkpoint-epoch{}.pth'.format(epoch))
        torch.save(state, filename)
        self.logger.info("Saving checkpoint: {} ...".format(filename))
        if save_best:
            best_path = str(self.checkpoint_dir / 'model_best.pth')
            torch.save(state, best_path)
            self.logger.info("Saving current best: model_best.pth ...")

    def _resume_checkpoint(self, resume_path):
        """
        Resume from saved checkpoints

        :param resume_path: Checkpoint path to be resumed
        """
        resume_path = str(resume_path)
        self.logger.info("Loading checkpoint: {} ...".format(resume_path))
        checkpoint = torch.load(resume_path)
        self.start_epoch = checkpoint['epoch'] + 1
        self.mnt_best = checkpoint['monitor_best']

        # load architecture params from checkpoint.
        if checkpoint['config']['arch'] != self.config['arch']:
            self.logger.warning("Warning: Architecture configuration given in config file is different from that of "
                                "checkpoint. This may yield an exception while state_dict is being loaded.")

        state_dict = checkpoint['state_dict']

        load_state_dict_keys = list(state_dict.keys())
        curr_state_dict_keys = list(self.model.state_dict().keys())
        redo_dp = False
        if not curr_state_dict_keys[0].startswith('module.') and load_state_dict_keys[0].startswith('module.'):
            undo_dp = True
        elif curr_state_dict_keys[0].startswith('module.') and not load_state_dict_keys[0].startswith('module.'):
            redo_dp = True
            undo_dp = False
        else:
            undo_dp = False

        if undo_dp:
            from collections import OrderedDict
            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = k[7:]  # remove `module.`
                new_state_dict[name] = v
            # load params
        elif redo_dp:
            from collections import OrderedDict
            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = 'module.' + k  # remove `module.`
                new_state_dict[name] = v
        else:
            new_state_dict = state_dict

        self.model.load_state_dict(new_state_dict)

        # load optimizer state from checkpoint only when optimizer type is not changed.
        if checkpoint['config']['optimizer']['type'] != self.config['optimizer']['type']:
            self.logger.warning("Warning: Optimizer type given in config file is different from that of checkpoint. "
                                "Optimizer parameters not being resumed.")
        else:
            self.optimizer.load_state_dict(checkpoint['optimizer'])

        self.logger.info("Checkpoint loaded. Resume training from epoch {}".format(self.start_epoch))
