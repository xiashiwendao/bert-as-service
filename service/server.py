#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Han Xiao <artex.xh@gmail.com> <https://hanxiao.github.io>

import multiprocessing
import os
import pickle
import threading
import time
from multiprocessing import Process

import tensorflow as tf
import zmq
from tensorflow.python.estimator.estimator import Estimator

from bert import tokenization, modeling
from bert.extract_features import model_fn_builder, convert_lst_to_features
from helper import set_logger

logger = set_logger()


class BertServer(threading.Thread):
    def __init__(self, args):
        super().__init__()
        self.model_dir = args.model_dir
        self.max_seq_len = args.max_seq_len
        self.num_worker = args.num_worker
        self.max_batch_size = args.batch_size_per_worker
        self.port = args.port
        self.args = args
        self.processes, self.workers = [], []
        self.frontend, self.backend, self.context = None, None, None

    def close(self):
        logger.info('shutting down bert-server...')
        for p in self.processes:
            p.close()
        self.frontend.close()
        self.backend.close()
        self.context.term()
        self.join()
        logger.info('bert-server is terminated!')

    def run(self):
        def get_a_worker():
            # w =
            # if not self.workers:
            #     # Don't poll clients if no workers are available
            #     poller.unregister(self.frontend)
            return self.workers.pop(0)

        def free_a_worker(w):
            # if not self.workers:
            #     # Poll for clients now that a worker is available
            #     poller.register(self.frontend, zmq.POLLIN)
            self.workers.append(w)

        def register_job(c, n):
            job_checksum[c] = n
            finish_jobs[c] = []

        def unregister_job(c):
            job_checksum.pop(c)
            finish_jobs.pop(c)

        self.context = zmq.Context.instance()
        self.frontend = self.context.socket(zmq.ROUTER)
        self.frontend.bind('tcp://*:%d' % self.port)
        self.backend = self.context.socket(zmq.ROUTER)
        self.backend.bind('ipc:///tmp/bert.service')

        available_gpus = range(self.num_worker)
        try:
            import GPUtil
            available_gpus = GPUtil.getAvailable(limit=self.num_worker)
            if len(available_gpus) < self.num_worker:
                logger.warning('only %d GPU(s) is available, but ask for %d' % (len(available_gpus), self.num_worker))
        except FileNotFoundError:
            logger.warn('nvidia-smi is missing, often means no gpu found on this machine. '
                        'will run service on cpu instead')

        for i in available_gpus:
            process = BertWorker(i, self.args)
            self.processes.append(process)
            process.start()

        poller = zmq.Poller()
        # Only poll for requests from backend until workers are available
        poller.register(self.backend, zmq.POLLIN)

        job_queue, finish_jobs, job_checksum = [], {}, {}

        while True:
            logger.info('available workers: %2d job queue: %3d' % (len(self.workers), len(job_queue)))
            sockets = dict(poller.poll(10))

            if self.backend in sockets:
                # Handle worker activity on the backend
                request = self.backend.recv_multipart()
                worker, _, client = request[:3]
                free_a_worker(worker)
                if client != b'READY' and len(request) > 3:
                    # If client reply, send rest back to frontend
                    _, reply = request[3:]
                    finish_jobs[client].extend(pickle.loads(reply))

            if self.frontend in sockets:
                # Get next client request, route to last-used worker
                client, _, request = self.frontend.recv_multipart()
                seqs = pickle.loads(request)
                num_seqs = len(seqs)
                register_job(client, num_seqs)

                s_idx = 0
                # divide the large batch into small batches
                while s_idx < num_seqs:
                    tmp = seqs[s_idx: (s_idx + self.max_batch_size)]
                    if tmp:
                        job_queue.append((client, tmp))
                    s_idx += len(tmp)

            # non-empty job queue and free workers, pop the last one and send it to a worker
            if job_queue:
                while self.workers:
                    client, tmp = job_queue.pop()
                    worker = get_a_worker()
                    self.backend.send_multipart([worker, b'', client, b'', pickle.dumps(tmp)])

            # check if there are finished jobs, send it back to workers
            for client, tmp in finish_jobs.items():
                if len(tmp) == job_checksum[client]:
                    self.frontend.send_multipart([client, b'', pickle.dumps(tmp)])
                    unregister_job(client)


class BertWorker(Process):
    def __init__(self, id, args):
        super().__init__()
        self.model_dir = args.model_dir
        self.config_fp = os.path.join(self.model_dir, 'bert_config.json')
        self.checkpoint_fp = os.path.join(self.model_dir, 'bert_model.ckpt')
        self.vocab_fp = os.path.join(args.model_dir, 'vocab.txt')
        self.tokenizer = tokenization.FullTokenizer(vocab_file=self.vocab_fp)
        self.max_seq_len = args.max_seq_len
        self.worker_id = id
        self.daemon = True
        self.model_fn = model_fn_builder(
            bert_config=modeling.BertConfig.from_json_file(self.config_fp),
            init_checkpoint=self.checkpoint_fp)
        os.environ['CUDA_VISIBLE_DEVICES'] = str(self.worker_id)
        self.estimator = Estimator(self.model_fn)
        self.result = []
        self.socket = None
        self.exit_flag = multiprocessing.Event()

    def close(self):
        logger.info('shutting down bert-worker %d ...' % self.worker_id)
        self.exit_flag.set()
        self.terminate()
        self.join()
        logger.info('bert-worker %d is terminated!' % self.worker_id)

    def run(self):
        self.socket = zmq.Context().socket(zmq.REQ)
        self.socket.identity = u'worker-{}'.format(self.worker_id).encode('ascii')
        self.socket.connect('ipc:///tmp/bert.service')

        input_fn = self.input_fn_builder(self.socket)
        self.socket.send(b'READY')
        logger.info('worker %d is ready and listening' % self.worker_id)
        for r in self.estimator.predict(input_fn):
            self.result.append([round(float(x), 6) for x in r.flat])

    @staticmethod
    def is_valid_input(texts):
        return isinstance(texts, list) and all(isinstance(s, str) for s in texts)

    def input_fn_builder(self, worker):
        def gen():
            while not self.exit_flag.is_set():
                if self.result:
                    num_result = len(self.result)
                    worker.send_multipart([ident, b'', pickle.dumps(self.result)])
                    self.result.clear()
                    time_used = time.perf_counter() - start
                    logger.info('encoded %d strs from %s in %.2fs @ %d/s' %
                                (num_result, ident, time_used, int(num_result / time_used)))
                ident, empty, msg = worker.recv_multipart()
                start = time.perf_counter()
                msg = pickle.loads(msg)
                if self.is_valid_input(msg):
                    tmp_f = list(convert_lst_to_features(msg, self.max_seq_len, self.tokenizer))
                    yield {
                        'input_ids': [f.input_ids for f in tmp_f],
                        'input_mask': [f.input_mask for f in tmp_f],
                        'input_type_ids': [f.input_type_ids for f in tmp_f]
                    }
                else:
                    logger.warning('worker %d: received unsupported type! sending back None' % self.id)
                    worker.send_multipart([ident, b'', pickle.dumps(None)])
            self.socket.close()

        def input_fn():
            return (tf.data.Dataset.from_generator(
                gen,
                output_types={k: tf.int32 for k in ['input_ids', 'input_mask', 'input_type_ids']},
                output_shapes={'input_ids': (None, self.max_seq_len),
                               'input_mask': (None, self.max_seq_len),
                               'input_type_ids': (None, self.max_seq_len)}))

        return input_fn
