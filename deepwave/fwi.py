import math
import torch
import deepwave

# dataset:
#   - num_shots
#   - get_shots(start, end)
#   - src_locs
#   - rec_locs
#   - dt
#   - dx


def extract_batch(dataset,
                  num_superbatches, num_batches, superbatch_idx,
                  batch_idx):
    num_shots = dataset.num_shots
    superbatch_size = math.ceil(num_shots / num_superbatches)
    batch_size = math.ceil(superbatch_size / num_batches)
    # 00.XX.00.00|00.AA.00.00|00.XX.00.00
    # ------------^ batch_idx * superbatch_size
    #             ---^ superbatch_idx * batch_size
    batch_start = min((batch_idx * superbatch_size +
                       superbatch_idx * batch_size),
                      num_shots)
    batch_end = min(batch_start + batch_size, num_shots)
    batch_slice = slice(batch_start, batch_end)
    batch_data = dataset.get_shots(batch_start, batch_end)
    batch_src_locs = dataset.src_locs[batch_slice]
    batch_rec_locs = dataset.rec_locs[batch_slice]

    return (torch.as_tensor(batch_data).float(),
            torch.as_tensor(batch_src_locs).float(),
            torch.as_tensor(batch_rec_locs).float())


def pool_data(data, num_pool, dt, start_time=0):
    #padding = int(start_time / dt) % int(num_pool / 2)  # t=0 starts a new block
    #padding = int(num_pool / 2)
    padding = 0
    pool = torch.nn.AvgPool1d(num_pool, padding=padding)
    data_pool = pool(data)
    return data_pool, dt * num_pool


def pool_model(model, dt, dx, min_cells_per_wavelength=4):
    ndims = model.dim()
    if ndims == 2:
        poolfunc = torch.nn.AvgPool1d
    elif ndims == 3:
        poolfunc = torch.nn.AvgPool2d
    elif ndims == 4:
        poolfunc = torch.nn.AvgPool3d
    else:
        raise ValueError
    min_vel = model.min().item()
    max_freq = 1 / dt / 2
    min_wavelength = min_vel / max_freq
    max_dx = min_wavelength / min_cells_per_wavelength
    num_pool = torch.ceil(max_dx / torch.as_tensor(dx)).long()
    pool = poolfunc(num_pool.tolist())
    model_pool = pool(model.reshape(1, *model.shape))[0]
    return model_pool, dx * num_pool.float()


def apply_max_time(batch_data_true, src_amp,
                   dt, src_start_time,
                   max_time):
    batch_data_true = batch_data_true[..., :max_time]
    max_src_amp_time = int(-src_start_time / dt + max_time)
    src_amp = src_amp[:max_src_amp_time]
    return batch_data_true, src_amp


def calc_survey_pad(max_time, dt, model, max_horiz_survey_pad):
    ndims = model.dim() - 1
    pad_dist = model.max().item() * max_time * dt / 2
    survey_pad = torch.ones(2 * ndims) * pad_dist
    if ndims > 1:
        survey_pad[2:] = survey_pad[2:].clamp(None, max_horiz_survey_pad)
    return survey_pad

# TODO:
# * invert increasing time
# * separate into train and validate datasets
# * use validate dataset to determine config changes
# * total variation regularization
# * supershots


def fwi(dataset, src_amp_init, src_start_time, model_init,
        num_pool_data, max_time,
        num_epochs, num_superbatches, num_batches, pml_width=10,
        max_horiz_survey_pad=500.0, lr_src_amp=0.0001, lr_model=1e5,
        weight_decay_src_amp=0.0, weight_decay_model=0.0,
        tv_model_amp=0.0,
        invert_source=True, invert_model=True, free_surface=False):

    # Check if GPU is available
    if torch.cuda.is_available():
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')

    # Convert inputs to PyTorch Tensors if they are not already
    src_amp_init = torch.as_tensor(src_amp_init).float()
    model_init = torch.as_tensor(model_init).float()
    print('A', model_init.shape)

    # Add extra dimension to model if needed
    if model_init.shape[0] != 1:
        model_init = model_init.reshape(1, *model_init.shape)
    print('B', model_init.shape)

    # Make copies of the initial source amplitude and model for updating
    # and send to GPU (if available)
    src_amp = src_amp_init.data.clone().to(device)
    if invert_source:
        src_amp.requires_grad_()
    #model = model_init.data.clone().to(device)
    model = torch.zeros_like(model_init.data).to(device)
    if invert_model:
        model.requires_grad_()
    print('C', model.shape)

    # Set-up inversion
    criterion = torch.nn.MSELoss()
    params = []
    if invert_source:
        params.append({'params': [src_amp], 'lr': lr_src_amp,
                       'weight_decay': weight_decay_src_amp})
    if invert_model:
        params.append({'params': [model], 'lr': lr_model,
                       'weight_decay': weight_decay_model})
    optimizer = torch.optim.Adam(params)
    pml_width = (torch.ones(6) * pml_width).long()
    pml_width[2 * (model_init.dim() - 1):] = 0
    if free_surface:
        pml_width[0] = 0
    tail = deepwave.utils.Tail()

    # Inversion loop
    #for max_time_idx in range(num_max_time):
    #    max_time = (max_time_idx + 1) * int(dataset.num_steps / num_max_time)
    survey_pad = calc_survey_pad(max_time, dataset.dt, # TODO: move to inner loop because of model?
                                 model_init, max_horiz_survey_pad)
    for epoch in range(num_epochs):
        epoch_loss = 0.0
        for superbatch_idx in range(num_superbatches):
            optimizer.zero_grad()
            for batch_idx in range(num_batches):
                # Extract batch of data
                batch_data_true, batch_src_locs, batch_rec_locs = \
                    extract_batch(dataset,
                                  num_superbatches, num_batches,
                                  superbatch_idx, batch_idx)
                epoch_loss += run_batch(dataset, src_amp, src_start_time,
                                        model, model_init, num_pool_data,
                                        batch_data_true,
                                        batch_src_locs, batch_rec_locs,
                                        max_time, device, pml_width,
                                        survey_pad, criterion, tail,
                                        tv_model_amp)
            optimizer.step()
        print('Epoch:', epoch, 'Loss: ', epoch_loss)
    print('D', model.shape, model_init.shape)

    return src_amp.detach(), (model + model_init).detach()


def run_batch(dataset, src_amp, src_start_time,
              model, model_init, num_pool_data,
              batch_data_true,
              batch_src_locs, batch_rec_locs,
              max_time, device, pml_width,
              survey_pad, criterion, tail,
              tv_model_amp):

    # Limit to maximum time
    batch_data_true, src_amp = apply_max_time(batch_data_true, src_amp,
                                              dataset.dt, src_start_time,
                                              max_time)

    # Pool data, source amplitude, and model
    batch_data_true, dt = pool_data(batch_data_true, num_pool_data,
                                    dataset.dt)
    src_amp_pool, _ = pool_data(src_amp.reshape(1, 1, -1),
                                num_pool_data, dataset.dt,
                                src_start_time)
    model_pool, dx = pool_model(model+model_init, dt, dataset.dx)

    # Make a copy of the source amplitude for each shot
    batch_src_amps = \
        src_amp_pool.reshape(-1, 1, 1)\
        .repeat(1, *batch_src_locs.shape[:2])

    # Move time to first axis of data
    batch_data_true = batch_data_true.permute(2, 0, 1)

    # Send to GPU (if available)
    batch_data_true = batch_data_true.to(device)
    batch_src_locs = batch_src_locs.to(device)
    batch_rec_locs = batch_rec_locs.to(device)

    # Create propagator
    prop = deepwave.scalar.Propagator(model_pool, dx,
                                      pml_width=pml_width,
                                      survey_pad=survey_pad)
    
    # Propagate and calculate loss
    batch_data_pred = prop(
        batch_src_amps, batch_src_locs, batch_rec_locs, dt)
    loss = criterion(*tail(batch_data_pred, batch_data_true)) + tv_model_amp * tvloss(model_pool, dx)
    loss.backward()
    return loss.detach().item()

def tvloss(model, dx):
    ndims = model.dim()
    tv1 = tv2 = tv3 = torch.zeros(1)
    if ndims >= 2:
        tv1 = torch.abs(model[0, 1:] - model[0, :-1]) / dx[0]
    if ndims >= 3:
        tv2 = torch.abs((model[0, :, 1:] - model[0, :, :-1])) / dx[1]
    if ndims == 4:
        tv3 = torch.abs((model[0, :, :, 1:] - model[0, :, :, :-1])) / dx[2]
    error = torch.norm(tv1, 1) + torch.norm(tv2, 1) + torch.norm(tv3, 1)
    return error
