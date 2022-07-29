import argparse
import itertools
import json
import math
import os
import sys

import numpy as np
import random

import pandas as pd
import torch
from torch import nn
from torch.optim import Adam

from Utils.base_train import batch_sampled_data, batching, inverse_output
from data.data_loader import ExperimentConfig
from models.rnn import RNN

erros = dict()
config_file = dict()


def train(args, model, train_en, train_de, train_y,
          test_en, test_de, test_y, epoch, e
          , val_loss, val_inner_loss, optimizer,
          config, config_num, best_config, criterion, path):

    stop = False
    try:
        total_loss = 0
        model.train()
        for batch_id in range(train_en.shape[0]):
            output = model(train_en[batch_id], train_de[batch_id])
            loss = criterion(output, train_y[batch_id])
            total_loss += loss.item()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        print("Train epoch: {}, loss: {:.4f}".format(epoch, total_loss))

        model.eval()
        test_loss = 0
        for j in range(test_en.shape[0]):
            outputs = model(test_en[j], test_de[j])
            loss = criterion(test_y[j], outputs)
            test_loss += loss.item()

        if test_loss < val_inner_loss:
            val_inner_loss = test_loss
            if val_inner_loss < val_loss:
                val_loss = val_inner_loss
                best_config = config
                torch.save({'model_state_dict': model.state_dict()}, os.path.join(path, "{}_{}".format(args.name, args.seed)))

            e = epoch

        if epoch - e > 10:
            stop = True

        print("Average loss: {:.4f}".format(test_loss))

    except KeyboardInterrupt:
        torch.save({
            'epoch': epoch,
            'model_state_dict': model.state_dict(),
            'config_num': config_num,
            'best_config': best_config
        }, os.path.join(path, "{}_continue".format(args.name)))
        sys.exit(0)

    return best_config, val_loss, val_inner_loss, stop, e


def evaluate(config, args, test_en, test_de, test_y, test_id, criterion, formatter, path, device):

    stack_size, d_model = config
    mae = nn.L1Loss()

    def extract_numerical_data(data):
        """Strips out forecast time and identifier columns."""
        return data[[
            col for col in data.columns
            if col not in {"forecast_time", "identifier"}
        ]]

    model = RNN(n_layers=stack_size,
                hidden_size=d_model,
                src_input_size=test_en.shape[3],
                tgt_input_size=test_de.shape[3],
                rnn_type="lstm",
                device=device,
                d_r=0,
                seed=args.seed)
    model.to(device)

    checkpoint = torch.load(os.path.join(path, "{}_{}".format(args.name, args.seed)))
    model.load_state_dict(checkpoint["model_state_dict"])

    model.eval()

    predictions = torch.zeros(test_y.shape[0], test_y.shape[1], test_y.shape[2])
    targets_all = torch.zeros(test_y.shape[0], test_y.shape[1], test_y.shape[2])

    for j in range(test_en.shape[0]):

        output = model(test_en[j], test_de[j])
        output_map = inverse_output(output, test_y[j], test_id[j])
        p = formatter.format_predictions(output_map["predictions"])
        if p is not None:
            forecast = torch.from_numpy(extract_numerical_data(p).to_numpy().astype('float32')).to(device)

            predictions[j, :forecast.shape[0], :] = forecast
            targets = torch.from_numpy(extract_numerical_data(
                formatter.format_predictions(output_map["targets"])).to_numpy().astype('float32')).to(device)

            targets_all[j, :targets.shape[0], :] = targets

    test_loss = criterion(predictions.to(device), targets_all.to(device)).item()
    normaliser = targets_all.to(device).abs().mean()
    test_loss = math.sqrt(test_loss) / normaliser

    mae_loss = mae(predictions.to(device), targets_all.to(device)).item()
    normaliser = targets_all.to(device).abs().mean()
    mae_loss = mae_loss / normaliser

    return test_loss, mae_loss


def create_config(hyper_parameters):
    prod = list(itertools.product(*hyper_parameters))
    return list(random.sample(set(prod), len(prod)))


def main():
    parser = argparse.ArgumentParser(description="preprocess argument parser")
    parser.add_argument("--name", type=str, default="lstm")
    parser.add_argument("--exp_name", type=str, default='electricity')
    parser.add_argument("--cuda", type=str, default="cuda:0")
    parser.add_argument("--seed", type=int, default=21)
    parser.add_argument("--total_time_steps", type=int, default=264)
    args = parser.parse_args()

    np.random.seed(args.seed)
    random.seed(args.seed)
    torch.manual_seed(args.seed)

    device = torch.device(args.cuda if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        print("Running on GPU")

    config = ExperimentConfig(args.exp_name)
    formatter = config.make_data_formatter()

    data_csv_path = "{}.csv".format(args.exp_name)

    print("Loading & splitting data_set...")
    raw_data = pd.read_csv(data_csv_path)
    train_data, valid, test = formatter.split_data(raw_data)
    train_max, valid_max = formatter.get_num_samples_for_calibration()
    params = formatter.get_experiment_params()
    params['total_time_steps'] = args.total_time_steps

    sample_data = batch_sampled_data(train_data, train_max, params['total_time_steps'],
                                     params['num_encoder_steps'], params["column_definition"], args.seed)
    train_en, train_de, train_y, train_id = torch.from_numpy(sample_data['enc_inputs']).to(device), \
                                            torch.from_numpy(sample_data['dec_inputs']).to(device), \
                                            torch.from_numpy(sample_data['outputs']).to(device), \
                                            sample_data['identifier']

    sample_data = batch_sampled_data(valid, valid_max, params['total_time_steps'],
                                     params['num_encoder_steps'], params["column_definition"], args.seed)
    valid_en, valid_de, valid_y, valid_id = torch.from_numpy(sample_data['enc_inputs']).to(device), \
                                            torch.from_numpy(sample_data['dec_inputs']).to(device), \
                                            torch.from_numpy(sample_data['outputs']).to(device), \
                                            sample_data['identifier']

    sample_data = batch_sampled_data(test, valid_max, params['total_time_steps'],
                                     params['num_encoder_steps'], params["column_definition"], args.seed)
    test_en, test_de, test_y, test_id = torch.from_numpy(sample_data['enc_inputs']).to(device), \
                                        torch.from_numpy(sample_data['dec_inputs']).to(device), \
                                        torch.from_numpy(sample_data['outputs']).to(device), \
                                        sample_data['identifier']

    model_params = formatter.get_default_model_params()

    seq_len = params['total_time_steps'] - params['num_encoder_steps']
    path = "models_{}_{}".format(args.exp_name, seq_len)
    if not os.path.exists(path):
        os.makedirs(path)

    criterion = nn.MSELoss()

    hyper_param = list([model_params['stack_size'],
                        model_params['hidden_layer_size']])
    configs = create_config(hyper_param)
    print('number of config: {}'.format(len(configs)))

    val_loss = 1e10
    best_config = configs[0]
    config_num = 0

    batch_size = model_params['minibatch_size'][0]

    train_en_p, train_de_p, train_y_p, train_id_p = batching(batch_size, train_en,
                                                             train_de, train_y, train_id)

    valid_en_p, valid_de_p, valid_y_p, valid_id_p = batching(batch_size, valid_en,
                                                             valid_de, valid_y, valid_id)

    test_en_p, test_de_p, test_y_p, test_id_p = batching(batch_size, test_en,
                                                         test_de, test_y, test_id)

    for i, conf in enumerate(configs, config_num):
        print('config {}: {}'.format(i+1, conf))

        stack_size, d_model = conf

        model = RNN(n_layers=stack_size,
                    hidden_size=d_model,
                    src_input_size=train_en_p.shape[3],
                    tgt_input_size=train_de_p.shape[3],
                    rnn_type="lstm",
                    device=device,
                    d_r=0,
                    seed=args.seed)
        model.to(device)

        optim = Adam(model.parameters())

        epoch_start = 0

        val_inner_loss = 1e10
        e = 0

        for epoch in range(epoch_start, params['num_epochs'], 1):

            best_config, val_loss, val_inner_loss, stop, e = \
                train(args, model, train_en_p.to(device), train_de_p.to(device),
                      train_y_p.to(device), valid_en_p.to(device), valid_de_p.to(device),
                      valid_y_p.to(device), epoch, e, val_loss, val_inner_loss,
                      optim, conf, i, best_config, criterion, path)

            if stop:
                break
        print("val loss: {:.4f}".format(val_inner_loss))
        del model

        print("best config so far: {}".format(best_config))

    test_loss, mae_loss = evaluate(best_config, args, test_en_p.to(device),
                                   test_de_p.to(device), test_y_p.to(device),
                                   test_id_p, criterion, formatter, path, device)

    stack_size, d_model = best_config
    print("best_config: {}".format(best_config))

    erros["{}_{}".format(args.name, args.seed)] = list()
    config_file["{}_{}".format(args.name, args.seed)] = list()
    erros["{}_{}".format(args.name, args.seed)].append(float("{:.5f}".format(test_loss)))
    erros["{}_{}".format(args.name, args.seed)].append(float("{:.5f}".format(mae_loss)))
    config_file["{}_{}".format(args.name, args.seed)] = list()
    config_file["{}_{}".format(args.name, args.seed)].append(d_model)

    print("test error for best config {:.4f}".format(test_loss))
    error_path = "errors_{}_{}.json".format(args.exp_name, seq_len)
    config_path = "configs_{}_{}.json".format(args.exp_name, seq_len)

    if os.path.exists(error_path):
        with open(error_path) as json_file:
            json_dat = json.load(json_file)
            if json_dat.get("{}_{}".format(args.name, args.seed)) is None:
                json_dat["{}_{}".format(args.name, args.seed)] = list()
            json_dat["{}_{}".format(args.name, args.seed)].append(float("{:.5f}".format(test_loss)))
            json_dat["{}_{}".format(args.name, args.seed)].append(float("{:.5f}".format(mae_loss)))

        with open(error_path, "w") as json_file:
            json.dump(json_dat, json_file)
    else:
        with open(error_path, "w") as json_file:
            json.dump(erros, json_file)

    if os.path.exists(config_path):
        with open(config_path) as json_file:
            json_dat = json.load(json_file)
            if json_dat.get("{}_{}".format(args.name, args.seed)) is None:
                json_dat["{}_{}".format(args.name, args.seed)] = list()
            json_dat["{}_{}".format(args.name, args.seed)].append(d_model)

        with open(config_path, "w") as json_file:
            json.dump(json_dat, json_file)
    else:
        with open(config_path, "w") as json_file:
            json.dump(config_file, json_file)


if __name__ == '__main__':
    main()