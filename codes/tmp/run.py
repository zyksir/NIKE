#!/usr/bin/python3

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import json
import logging
import os
import random
import pickle
import numpy as np
import torch
torch.set_num_threads(8)
from torch.utils.data import DataLoader
from IPython import embed
from model import KGEModel, SimpleNN
from tqdm import tqdm
from dataloader import TrainDataset, BidirectionalOneShotIterator


def RotatE(head, relation, tail, mode, embed_model):
    pi = 3.14159265358979323846

    re_head, im_head = torch.chunk(head, 2, dim=2)  # (batch_size, negative_sample_size, hidden_dim)
    re_tail, im_tail = torch.chunk(tail, 2, dim=2)

    # Make phases of relations uniformly distributed in [-pi, pi]

    phase_relation = relation / (embed_model.embedding_range.item() / pi)

    re_relation = torch.cos(phase_relation)
    im_relation = torch.sin(phase_relation)

    if mode == 'head-batch':
        re_score = re_relation * re_tail + im_relation * im_tail
        im_score = re_relation * im_tail - im_relation * re_tail
        re_score = re_score - re_head
        im_score = im_score - im_head
    else:
        re_score = re_head * re_relation - im_head * im_relation
        im_score = re_head * im_relation + im_head * re_relation
        re_score = re_score - re_tail
        im_score = im_score - im_tail

    score = torch.stack([re_score, im_score], dim=0)  # (batch_size, negative_sample_size, hidden_dim)
    score = score.norm(dim=0)
    return score

def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description='Training and Testing Knowledge Graph Embedding Models',
        usage='train.py [<args>] [-h | --help]'
    )

    parser.add_argument('--num', type=int, default=1)
    parser.add_argument('--cuda', action='store_true', help='use GPU')
    parser.add_argument('--no_save', action='store_true', help='do not save models')
    parser.add_argument('--train_set', default='train.txt', help='file for train')
    parser.add_argument('--method', type=str, default=None)
    parser.add_argument("--fake", type=str, default=None)
    parser.add_argument('--do_train', action='store_true')
    parser.add_argument('--do_valid', action='store_true')
    parser.add_argument('--do_test', action='store_true')
    parser.add_argument('--evaluate_train', action='store_true', help='Evaluate on training data')

    parser.add_argument('--countries', action='store_true', help='Use Countries S1/S2/S3 datasets')
    parser.add_argument('--regions', type=int, nargs='+', default=None,
                        help='Region Id for Countries S1/S2/S3 datasets, DO NOT MANUALLY SET')

    parser.add_argument('--data_path', type=str, default=None)
    parser.add_argument('--model', default='TransE', type=str)
    parser.add_argument('-de', '--double_entity_embedding', action='store_true')
    parser.add_argument('-dr', '--double_relation_embedding', action='store_true')

    parser.add_argument('-n', '--negative_sample_size', default=128, type=int)
    parser.add_argument('-d', '--hidden_dim', default=500, type=int)
    parser.add_argument('--gen_dim', default=250, type=int)
    parser.add_argument('-g', '--gamma', default=12.0, type=float)
    parser.add_argument('-adv', '--negative_adversarial_sampling', action='store_true')
    parser.add_argument('-a', '--adversarial_temperature', default=1.0, type=float)
    parser.add_argument('-b', '--batch_size', default=1024, type=int)
    parser.add_argument('-r', '--regularization', default=0.0, type=float)
    parser.add_argument('--test_batch_size', default=4, type=int, help='valid/test batch size')
    parser.add_argument('--uni_weight', action='store_true',
                        help='Otherwise use subsampling weighting like in word2vec')

    parser.add_argument('-lr', '--learning_rate', default=0.0001, type=float)
    parser.add_argument('-cpu', '--cpu_num', default=10, type=int)
    parser.add_argument('-init', '--init_checkpoint', default=None, type=str)
    parser.add_argument('--gen_init', default=None, type=str)
    parser.add_argument('-save', '--save_path', default=None, type=str)
    parser.add_argument('--max_steps', default=100000, type=int)
    parser.add_argument('--warm_up_steps', default=None, type=int)

    parser.add_argument('--save_checkpoint_steps', default=10000, type=int)
    parser.add_argument('--valid_steps', default=10000, type=int)
    parser.add_argument('--log_steps', default=100, type=int, help='train log every xx steps')
    parser.add_argument('--test_log_steps', default=1000, type=int, help='valid/test log every xx steps')

    parser.add_argument('--nentity', type=int, default=0, help='DO NOT MANUALLY SET')
    parser.add_argument('--nrelation', type=int, default=0, help='DO NOT MANUALLY SET')

    return parser.parse_args(args)


def override_config(args):
    '''
    Override model and data configuration
    '''

    with open(os.path.join(args.init_checkpoint, 'config.json'), 'r') as fjson:
        argparse_dict = json.load(fjson)

    args.countries = argparse_dict['countries']
    if args.data_path is None:
        args.data_path = argparse_dict['data_path']
    args.model = argparse_dict['model']
    args.double_entity_embedding = argparse_dict['double_entity_embedding']
    args.double_relation_embedding = argparse_dict['double_relation_embedding']
    args.hidden_dim = argparse_dict['hidden_dim']
    args.test_batch_size = argparse_dict['test_batch_size']
    args.fake = argparse_dict['fake']
    if not args.do_train:
        args.method = argparse_dict['method']
        args.save_path = argparse_dict['save_path']


def save_model(model, optimizer, save_variable_list, args, classifier=None, generator=None):
    '''
    Save the parameters of the model and the optimizer,
    as well as some other variables such as step and learning_rate
    '''
    if args.no_save:
        return
    argparse_dict = vars(args)
    with open(os.path.join(args.save_path, 'config.json'), 'w') as fjson:
        json.dump(argparse_dict, fjson)

    checkpoint = {
        **save_variable_list,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict()}
    if classifier is not None:
        checkpoint['classifier_state_dict'] = classifier.state_dict()
    if generator is not None:
        checkpoint['generator_state_dict'] = generator.state_dict()
    torch.save(checkpoint, os.path.join(args.save_path, 'checkpoint'))
    entity_embedding = model.entity_embedding.detach().cpu().numpy()
    # np.save(os.path.join(args.save_path, 'entity_embedding'), entity_embedding)

    relation_embedding = model.relation_embedding.detach().cpu().numpy()
    # np.save(os.path.join(args.save_path, 'relation_embedding'), relation_embedding)


def read_triple(file_path, entity2id, relation2id):
    '''
    Read triples and map them into ids.
    '''
    triples = []
    with open(file_path) as fin:
        for line in fin:
            h, r, t = line.strip().split('\t')
            triples.append((entity2id[h], relation2id[r], entity2id[t]))
    return triples


def set_logger(args):
    '''
    Write logs to checkpoint and console
    '''

    if args.do_train:
        log_file = os.path.join(args.save_path or args.init_checkpoint, 'train.log')
    else:
        log_file = os.path.join(args.save_path or args.init_checkpoint, 'test.log')

    if os.path.exists(log_file):
        command = input("log file exists in %s, are you sure you want to override it?(Y/N)" % log_file)
        if command.lower() == "n":
            exit(0)

    logging.basicConfig(
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S',
        filename=log_file,
        filemode='w'
    )
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)


def log_metrics(mode, step, metrics):
    '''
    Print the evaluation logs
    '''
    for metric in metrics:
        logging.info('%s %s at step %d: %f' % (mode, metric, step, metrics[metric]))


def main(args):
    if (not args.do_train) and (not args.do_valid) and (not args.do_test):
        raise ValueError('one of train/val/test mode must be choosed.')

    if args.init_checkpoint:
        override_config(args)
    elif args.data_path is None:
        raise ValueError('one of init_checkpoint/data_path must be choosed.')

    if args.do_train and args.save_path is None:
        raise ValueError('Where do you want to save your trained model?')

    if args.save_path and not os.path.exists(args.save_path):
        os.makedirs(args.save_path)

    # Write logs to checkpoint and console
    set_logger(args)

    with open(os.path.join(args.data_path, 'entities.dict')) as fin:
        entity2id = dict()
        for line in fin:
            eid, entity = line.strip().split('\t')
            entity2id[entity] = int(eid)

    with open(os.path.join(args.data_path, 'relations.dict')) as fin:
        relation2id = dict()
        for line in fin:
            rid, relation = line.strip().split('\t')
            relation2id[relation] = int(rid)

    nentity = len(entity2id)
    nrelation = len(relation2id)

    args.nentity = nentity
    args.nrelation = nrelation

    logging.info('Model: %s' % args.model)
    logging.info('Data Path: %s' % args.data_path)
    logging.info('#entity: %d' % nentity)
    logging.info('#relation: %d' % nrelation)

    train_triples = read_triple(os.path.join(args.data_path, args.train_set), entity2id, relation2id)
    if args.fake:
        fake_triples = pickle.load(open(os.path.join(args.data_path, "fake%s.pkl" % args.fake), "rb"))
        fake = torch.LongTensor(fake_triples)
        train_triples += fake_triples
    else:
        fake_triples = [(0, 0, 0)]
        fake = torch.LongTensor(fake_triples)
    if args.cuda:
        fake = fake.cuda()
    logging.info('#train: %d' % len(train_triples))
    valid_triples = read_triple(os.path.join(args.data_path, 'valid.txt'), entity2id, relation2id)
    logging.info('#valid: %d' % len(valid_triples))
    test_triples = read_triple(os.path.join(args.data_path, 'test.txt'), entity2id, relation2id)
    logging.info('#test: %d' % len(test_triples))

    all_true_triples = train_triples + valid_triples + test_triples

    kge_model = KGEModel(
        model_name=args.model,
        nentity=nentity,
        nrelation=nrelation,
        hidden_dim=args.hidden_dim,
        gamma=args.gamma,
        double_entity_embedding=args.double_entity_embedding,
        double_relation_embedding=args.double_relation_embedding
    )

    logging.info('Model Parameter Configuration:')
    for name, param in kge_model.named_parameters():
        logging.info('Parameter %s: %s, require_grad = %s' % (name, str(param.size()), str(param.requires_grad)))
    if args.cuda:
        kge_model = kge_model.cuda()

    # Set training dataloader iterator
    train_dataset_head = TrainDataset(train_triples, nentity, nrelation, args.negative_sample_size, 'head-batch')
    train_dataset_tail = TrainDataset(train_triples, nentity, nrelation, args.negative_sample_size, 'tail-batch')
    for triple in tqdm(train_dataset_head.triples, total=len(train_dataset_head.triples)):
        train_dataset_head.subsampling_weights[triple] = torch.FloatTensor([1.0])
    train_dataset_tail.subsampling_weights = train_dataset_head.subsampling_weights

    train_dataloader_head = DataLoader(
        train_dataset_head,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=max(1, args.cpu_num // 2),
        collate_fn=TrainDataset.collate_fn
    )

    train_dataloader_tail = DataLoader(
        train_dataset_tail,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=max(1, args.cpu_num // 2),
        collate_fn=TrainDataset.collate_fn
    )

    train_iterator = BidirectionalOneShotIterator(train_dataloader_head, train_dataloader_tail)
    classifier, generator = None, None
    if args.method == "clf" or args.method is None:
        args.gen_dim = args.hidden_dim
        clf_triples = random.sample(train_triples, len(train_triples)//10)
        clf_dataset_head = TrainDataset(clf_triples, nentity, nrelation,
                                        args.negative_sample_size, 'head-batch')
        clf_dataset_tail = TrainDataset(clf_triples, nentity, nrelation,
                                        args.negative_sample_size, 'tail-batch')
        clf_dataset_head.true_head, clf_dataset_head.true_tail = train_dataset_head.true_head, train_dataset_head.true_tail
        clf_dataset_tail.true_head, clf_dataset_tail.true_tail = train_dataset_tail.true_head, train_dataset_tail.true_tail
        clf_dataset_head.subsampling_weights = train_dataset_head.subsampling_weights
        clf_dataset_tail.subsampling_weights = train_dataset_head.subsampling_weights
        clf_dataloader_head = DataLoader(
            clf_dataset_head,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TrainDataset.collate_fn
        )

        clf_dataloader_tail = DataLoader(
            clf_dataset_tail,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TrainDataset.collate_fn
        )
        clf_iterator = BidirectionalOneShotIterator(clf_dataloader_head, clf_dataloader_tail)

        gen_dataset_head = TrainDataset(clf_triples, nentity, nrelation,
                                        args.negative_sample_size, 'head-batch')
        gen_dataset_tail = TrainDataset(clf_triples, nentity, nrelation,
                                        args.negative_sample_size, 'tail-batch')
        gen_dataset_head.true_head, gen_dataset_head.true_tail = train_dataset_head.true_head, train_dataset_head.true_tail
        gen_dataset_tail.true_head, gen_dataset_tail.true_tail = train_dataset_tail.true_head, train_dataset_tail.true_tail
        gen_dataset_head.subsampling_weights = train_dataset_head.subsampling_weights
        gen_dataset_tail.subsampling_weights = train_dataset_head.subsampling_weights
        gen_dataloader_head = DataLoader(
            gen_dataset_head,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TrainDataset.collate_fn
        )

        gen_dataloader_tail = DataLoader(
            gen_dataset_tail,
            batch_size=args.batch_size,
            shuffle=True,
            num_workers=max(1, args.cpu_num // 2),
            collate_fn=TrainDataset.collate_fn
        )
        gen_iterator = BidirectionalOneShotIterator(gen_dataloader_head, gen_dataloader_tail)

        # if args.double_entity_embedding:
        #     classifier = SimpleNN(input_dim=args.hidden_dim, hidden_dim=5)
        #     generator = SimpleNN(input_dim=args.hidden_dim, hidden_dim=5)
        # else:
        classifier = SimpleNN(input_dim=args.hidden_dim, hidden_dim=5)
        generator = SimpleNN(input_dim=args.hidden_dim, hidden_dim=5)

        if args.cuda:
            classifier = classifier.cuda()
            generator = generator.cuda()
        clf_lr = 0.005 # if "FB15k" in args.data_path else 0.01
        clf_opt = torch.optim.Adam(classifier.parameters(), lr=clf_lr)
        gen_opt = torch.optim.SGD(generator.parameters(), lr=0.0001)
    elif args.method == "KBGAN":
        generator = KGEModel(
            model_name=args.model,
            nentity=nentity,
            nrelation=nrelation,
            hidden_dim=args.gen_dim,
            gamma=args.gamma,
            double_entity_embedding=args.double_entity_embedding,
            double_relation_embedding=args.double_relation_embedding
        )
        if args.cuda:
            generator = generator.cuda()
        # if args.gen_init is not None:
        #     checkpoint = torch.load(os.path.join(args.gen_init, 'checkpoint'))
        #     generator.load_state_dict(checkpoint['model_state_dict'])
        gen_opt = torch.optim.Adam(generator.parameters(), lr=args.learning_rate)

    # Set training configuration
    current_learning_rate = args.learning_rate
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, kge_model.parameters()),
        lr=current_learning_rate
    )
    if args.warm_up_steps:
        warm_up_steps = args.warm_up_steps
    else:
        warm_up_steps = args.max_steps # // 2

    if args.init_checkpoint:
        # Restore model from checkpoint directory
        logging.info('Loading checkpoint %s...' % args.init_checkpoint)
        checkpoint = torch.load(os.path.join(args.init_checkpoint, 'checkpoint'))
        init_step = 0
        kge_model.load_state_dict(checkpoint['model_state_dict'])
        if args.do_train:
            warm_up_steps = checkpoint['warm_up_steps']
            logging.info("warm_up_steps = %d" % warm_up_steps)
        else:
            current_learning_rate = args.learning_rate
    else:
        logging.info('Ramdomly Initializing %s Model...' % args.model)
        init_step = 0

    step = init_step

    logging.info('Start Training...')
    logging.info('init_step = %d' % init_step)
    logging.info('learning_rate = %d' % current_learning_rate)
    logging.info('batch_size = %d' % args.batch_size)
    logging.info('negative_adversarial_sampling = %d' % args.negative_adversarial_sampling)
    logging.info('hidden_dim = %d' % args.hidden_dim)
    logging.info('gamma = %f' % args.gamma)
    logging.info('negative_adversarial_sampling = %s' % str(args.negative_adversarial_sampling))
    if args.negative_adversarial_sampling:
        logging.info('adversarial_temperature = %f' % args.adversarial_temperature)

    # Set valid  as it would be evaluated during training
    if args.do_train:
        if args.method == "clf" and args.init_checkpoint:
            # classifier.find_topK_triples(kge_model, classifier, train_iterator, clf_iterator, GAN_iterator)
            # logging.info("fake triples in classifier training %d / %d" % (
            #     len(set(fake_triples).intersection(set(clf_iterator.dataloader_head.dataset.triples))),
            #     len(clf_iterator.dataloader_head.dataset.triples)))
            for epoch in range(1200):
                log = classifier.train_classifier_step(kge_model, classifier, clf_opt, clf_iterator, args, generator=None, model_name=args.model)
                if (epoch+1) % 200 == 0:
                    logging.info(log)
                if epoch == 4000:
                    clf_opt = torch.optim.Adam(classifier.parameters(), lr=clf_lr/10)
            clf_opt = torch.optim.Adam(classifier.parameters(), lr=clf_lr)


        training_logs = []

        # Training Loop
        logging.info(optimizer)
        soft = False
        epoch_reward, epoch_loss, avg_reward, log = 0, 0, 0, {}
        for step in range(init_step, args.max_steps):
            if args.method == "clf" and step % 10001 == 0:
                if args.num == 1:
                    soft = True
                elif args.num == 1000:
                    soft = False
                else:
                    soft = not soft
                head, relation, tail = classifier.get_embedding(kge_model, fake)
                if args.model == "RotatE":
                    fake_score = classifier.forward(RotatE(head, relation, tail, "single", kge_model))
                elif args.model == "DistMult":
                    fake_score = classifier.forward(head * relation * tail)
                elif args.model == "TransE":
                    fake_score = classifier.forward(head + relation - tail)
                all_weight = classifier.find_topK_triples(kge_model, classifier, train_iterator, clf_iterator,
                                                           gen_iterator, soft=soft, model_name=args.model)
                logging.info("fake percent %f in %d" % (fake_score.sum().item() / all_weight, all_weight))
                logging.info("fake triples in classifier training %d / %d" % (
                    len(set(fake_triples).intersection(set(clf_iterator.dataloader_head.dataset.triples))),
                    len(clf_iterator.dataloader_head.dataset.triples)))

                epoch_reward, epoch_loss, avg_reward = 0, 0, 0
                for epoch in tqdm(range(200)):
                    classifier.train_GAN_step(kge_model, generator, classifier, gen_opt, clf_opt, gen_iterator, epoch_reward, epoch_loss, avg_reward, args, model_name=args.model)

                clf_train_num = 200
                for epoch in range(clf_train_num):
                    log = classifier.train_classifier_step(kge_model, classifier, clf_opt, clf_iterator, args, generator=None, model_name=args.model)
                    if epoch % 100 == 0:
                        logging.info(log)

            if step % 300 == 0 and step > 0 and args.method == "KBGAN":
                avg_reward = epoch_reward / batch_num
                epoch_reward, epoch_loss = 0, 0
                logging.info('Training average reward at step %d: %f' % (step, avg_reward))
                logging.info('Training average loss at step %d: %f' % (step, epoch_loss / batch_num))

            if args.method == "KBGAN":
                epoch_reward, epoch_loss, batch_num = kge_model.train_GAN_step(generator, kge_model, gen_opt, optimizer, train_iterator, epoch_reward, epoch_loss, avg_reward, args)
            else:
                log = kge_model.train_step(kge_model, optimizer, train_iterator, args, generator=generator)

            training_logs.append(log)

            if step >= warm_up_steps:
                current_learning_rate = current_learning_rate / 10
                logging.info('Change learning_rate to %f at step %d' % (current_learning_rate, step))
                optimizer = torch.optim.Adam(
                    filter(lambda p: p.requires_grad, kge_model.parameters()),
                    lr=current_learning_rate
                )
                warm_up_steps = warm_up_steps * 3

            if step % args.save_checkpoint_steps == 0:
                save_variable_list = {
                    'step': step,
                    'current_learning_rate': current_learning_rate,
                    'warm_up_steps': warm_up_steps
                }
                if args.method is not None:
                    save_variable_list["confidence"] = train_iterator.dataloader_head.dataset.subsampling_weights
                save_model(kge_model, optimizer, save_variable_list, args, classifier=classifier, generator=generator)

            if step % args.log_steps == 0:
                metrics = {}
                for metric in training_logs[0].keys():
                    metrics[metric] = sum([log[metric] for log in training_logs]) / len(training_logs)
                log_metrics('Training average', step, metrics)
                training_logs = []

            if args.do_valid and step % args.valid_steps == 0:
                logging.info('Evaluating on Valid Dataset...')
                metrics = kge_model.test_step(kge_model, valid_triples, all_true_triples, args)
                log_metrics('Valid', step, metrics)
        save_variable_list = {
            'step': step,
            'current_learning_rate': current_learning_rate,
            'warm_up_steps': warm_up_steps
        }
        if args.method is not None:
            save_variable_list["confidence"] = train_iterator.dataloader_head.dataset.subsampling_weights
        save_model(kge_model, optimizer, save_variable_list, args, classifier=classifier, generator=generator)

    if args.do_valid:
        logging.info('Evaluating on Valid Dataset...')
        metrics = kge_model.test_step(kge_model, valid_triples, all_true_triples, args)
        log_metrics('Valid', step, metrics)

    if args.do_test:
        logging.info('Evaluating on Test Dataset...')
        metrics = kge_model.test_step(kge_model, test_triples, all_true_triples, args)
        log_metrics('Test', step, metrics)
        if args.method is not None:
            classifier.find_topK_triples(kge_model, classifier, train_iterator, clf_iterator,
                                         gen_iterator, soft=True, model_name=args.model)
            # torch.save(train_iterator.dataloader_head.dataset.subsampling_weights,
            #            os.path.join(args.save_path, 'weight'))
            true_triples = set(train_triples) - set(fake_triples)
            scores, label = [], []
            for triple in true_triples:
                if not (triple == (0, 0, 0)):
                    scores.append(train_iterator.dataloader_head.dataset.subsampling_weights[triple].item())
                    label.append(1)
            for triple in fake_triples:
                if not (triple == (0, 0, 0)):
                    scores.append(train_iterator.dataloader_head.dataset.subsampling_weights[triple].item())
                    label.append(0)
        else:
            print("start to use sigmoid to translate distance to probability")
            scores, label = [], []
            true_triples = set(train_triples) - set(fake_triples)
            i = 0
            import sys
            while i < len(train_iterator.dataloader_head.dataset.triples):
                sys.stdout.write("%d in %d\r" % (i, len(train_iterator.dataloader_head.dataset.triples)))
                sys.stdout.flush()
                j = min(i + 1024, len(train_iterator.dataloader_head.dataset.triples))
                sample = torch.LongTensor(train_iterator.dataloader_head.dataset.triples[i: j]).cuda()
                score = kge_model(sample).detach().cpu().view(-1)
                for x, triple in enumerate(train_iterator.dataloader_head.dataset.triples[i: j]):
                    if triple in true_triples:
                        label.append(1)
                        scores.append(torch.sigmoid(score[x]))
                    elif triple in fake_triples:
                        label.append(0)
                        scores.append(torch.sigmoid(score[x]))
                i = j
                del sample
                del score
        scores, label = np.array(scores), np.array(label)
        from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score
        p = precision_score(label, scores > 0.5)
        r = recall_score(label, scores > 0.5)
        f1 = f1_score(label, scores > 0.5)
        auc = roc_auc_score(label, scores > 0.5)
        logging.info(f"""
        precision = {p}
        recall = {r}
        f1 score = {f1}
        auc score = {auc}
        """)
        p = precision_score(1 - label, scores < 0.5)
        r = recall_score(1 - label, scores < 0.5)
        f1 = f1_score(1 - label, scores < 0.5)
        auc = roc_auc_score(1 - label, scores < 0.5)
        logging.info(f"""
                precision = {p}
                recall = {r}
                f1 score = {f1}
                auc score = {auc}
                """)

    if args.evaluate_train:
        logging.info('Evaluating on Training Dataset...')
        metrics = kge_model.test_step(kge_model, train_triples, all_true_triples, args)
        log_metrics('Test', step, metrics)


if __name__ == '__main__':
    main(parse_args())