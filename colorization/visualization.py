import os
import re
from functools import partial
from itertools import chain
from operator import itemgetter

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import torch
from matplotlib.lines import Line2D
from torch.utils.data import DataLoader
from torchvision.models import vgg16
from torchvision import transforms

from .cielab import DEFAULT_CIELAB
from .data.image_directory import ImageDirectory
from .data.transforms import PredictColor
from .util.image import imread, rgb_to_lab


_FIGURE_WIDTH = 12
_FIGURE_SPACING = 0.05


def _subplots(r=1, c=1, use_gridspec=False, no_ticks=True):
    display_width = (_FIGURE_WIDTH - (c - 1) * _FIGURE_SPACING) / c
    figure_height = r * display_width + (r - 1) * _FIGURE_SPACING

    if use_gridspec:
        fig = plt.figure(figsize=(_FIGURE_WIDTH, figure_height))

        gs = gridspec.GridSpec(r, c)
        gs.update(wspace=_FIGURE_SPACING, hspace=_FIGURE_SPACING)

        axes = np.empty((r, c), dtype=np.object)

        for r_ in range(r):
            for c_ in range(c):
                axes[r_, c_] = plt.subplot(gs[r_ * c + c_])
    else:
        fig, axes = plt.subplots(r, c, figsize=(_FIGURE_WIDTH, figure_height))

        if not (r == 1 and c == 1):
            axes = axes.reshape(r, c)

    if no_ticks:
        for ax in ([axes] if r == c == 1 else axes.flatten()):
            ax.tick_params(
                axis='both',
                which='both',
                bottom=False,
                top=False,
                left=False,
                right=False,
                labelbottom=False,
                labeltop=False,
                labelleft=False,
                labelright=False)

    return fig, axes


def _bbox(fig, ax):
    r = fig.canvas.get_renderer()

    return ax.get_tightbbox(r).transformed(fig.transFigure.inverted())


def _subplot_divider(fig, axes, orientation, n):
    bbox = partial(_bbox, fig)

    line2d = partial(Line2D,
                     transform=fig.transFigure,
                     color='k',
                     linestyle='--')

    if orientation == 'horizontal':
        y = (bbox(axes[n, 0]).y1 + bbox(axes[n + 1, 0]).y0) / 2
        line = line2d([0, 1], [y, y])
    elif orientation == 'vertical':
        x = (bbox(axes[0, n]).x1 + bbox(axes[0, n + 1]).x0) / 2
        line = line2d([x, x], [0, 1])
    else:
        raise ValueError("orientation must be 'horizontal' or 'vertical'")

    fig.add_artist(line)


def _dataloader(image_dir, labeled=False, transform=None):
    dataset = ImageDirectory(image_dir, labeled=labeled, transform=transform)

    return DataLoader(dataset)


def _torch_to_numpy(img):
    return img[0, :, :, :].numpy()


def _display_progress(i, i_end, msg='processing image'):
    fmt = "{} {}/{}"

    ljust = len(fmt.format(msg, i_end, i_end))

    end = '\n' if i == i_end - 1 else ''

    print('\r' + fmt.format(msg, i + 1, i_end).ljust(ljust), end=end)


def _raw_accuracy(ab_pred, ab_label, thresh, reweigh_classes=False):
    if reweigh_classes:
        # bin ground truth pixels
        q = DEFAULT_CIELAB.bin_ab(ab_pred)

        # get pixel weights
        pixel_weights = 1 / DEFAULT_CIELAB.gamut.prior[q]
        pixel_weights /= pixel_weights.sum()

    # find pixels not exceeding threshold distance
    dist = np.linalg.norm(ab_label - ab_pred, axis=2)

    within_thresh = dist <= thresh
    num_within_thresh = np.count_nonzero(within_thresh)

    if num_within_thresh == 0:
        return 0

    if reweigh_classes:
        return (within_thresh * pixel_weights).sum()
    else:
        return num_within_thresh / np.prod(within_thresh.shape[:2])


def _auc(ab_pred,
         ab_label,
         thresh_min=0,
         thresh_max=151,
         thresh_step=10,
         reweigh_classes=False):

    thresh = np.arange(thresh_min, thresh_max, thresh_step)

    raw_acc = partial(_raw_accuracy,
                      ab_pred,
                      ab_label,
                      reweigh_classes=reweigh_classes)

    aucs = [raw_acc(t) for t in thresh]

    return sum(aucs) / len(thresh)


def learning_curve_from_log(filename,
                            loss_regex,
                            smoothing_alpha=0.05,
                            ax=None):

    if ax is None:
        _, ax = _subplots(no_ticks=False)

    # create loss curve
    losses = []
    with open(filename, 'r') as f:
        for line in f:
            m = re.search(loss_regex, line)

            if m is not None:
                loss = float(m.group(1))
                losses.append(loss)

    iterations = np.arange(1, len(losses) + 1)

    # create smoothed loss curve
    losses_smoothed = losses[:1]
    for loss in losses[1:]:
        loss_smoothed = smoothing_alpha * loss + \
                        (1 - smoothing_alpha) * losses_smoothed[-1]

        losses_smoothed.append(loss_smoothed)

    # plot loss curves
    p = ax.semilogy(iterations, losses, alpha=.5)
    ax.semilogy(iterations, losses_smoothed, color=p[0].get_color())

    # format plot
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Loss (Log)")

    ax.grid()


def annealed_mean_demo(model, image_dir, ts=None, verbose=False):
    dataloader = _dataloader(image_dir)

    # temperature parameters
    t_orig = model.network.decode_q.T

    if ts is None:
        ts = [1, .77, .58, .38, .29, .14, 0]

    assert len(ts) % 2 == 1

    # run predictions
    _, axes = _subplots(len(dataloader), len(ts), use_gridspec=True)

    for c, t in enumerate(ts):
        if verbose:
            print("running prediction for T = {}".format(t))

        model.network.decode_q.T = t

        for r, img in enumerate(dataloader):
            axes[r, c].imshow(PredictColor(model)(_torch_to_numpy(img)))

    # reset temperature parameter
    model.network.decode_q.T = t_orig

    # add titles
    for i, (t, ax) in enumerate(zip(ts, axes[0, :])):
        suptitle = {
            0: "Mean",
            len(ts) // 2: "Annealed Mean",
            len(ts) - 1: "Mode"
        }.get(i, '')

        if t == 0:
            title_fmt = "{}\n$T\\rightarrow{}$"
        else:
            title_fmt = "{}\n$T={}$"

        ax.set_title(title_fmt.format(suptitle, t))


def good_vs_bad_demo(model_norebal,
                     model_rebal,
                     image_dir_good,
                     image_dir_bad,
                     verbose=False):

    dataloader_good = _dataloader(image_dir_good)
    dataloader_bad = _dataloader(image_dir_bad)

    assert len(dataloader_good) % 2 == 1
    assert len(dataloader_bad) % 2 == 1

    fig, axes = _subplots(
        len(dataloader_good) + len(dataloader_bad), 4, use_gridspec=False)

    for r, img in enumerate(chain(dataloader_good, dataloader_bad)):
        if verbose:
            print("running prediction for image {}".format(r + 1))

        # input
        axes[r, 0].imshow(rgb_to_lab(_torch_to_numpy(img))[:, :, 0], cmap='gray')

        # prediction
        axes[r, 1].imshow(PredictColor(model_norebal)(_torch_to_numpy(img)))

        # prediction (with class rebalancing)
        axes[r, 2].imshow(PredictColor(model_rebal)(_torch_to_numpy(img)))

        # ground truth
        axes[r, 3].imshow(_torch_to_numpy(img))

    # plot divider
    plt.tight_layout()

    _subplot_divider(fig, axes, 'horizontal', len(dataloader_good) - 1)

    # add titles
    axes[0, 0].set_title("Input")
    axes[0, 1].set_title("Classification")
    axes[0, 2].set_title("Classification\n(w/ Rebalancing)")
    axes[0, 3].set_title("Ground Truth")


def amt_results_demo(model,
                     results_dir,
                     rows=4,
                     columns_best=3,
                     columns_worst=1,
                     verbose=False):

    # parse results
    results = {}

    with open(os.path.join(results_dir, 'results.txt'), 'r') as f:
        for line in f:
            path, res = line.split()

            results[os.path.join(results_dir, path)] = float(res)

    assert len(results) >= rows * (columns_worst + columns_best)

    images_sorted = [
        path for path, _ in sorted(results.items(), key=itemgetter(1))
    ]

    # plot results
    fig, axes = _subplots(rows, (columns_best + columns_worst) * 2)

    images_show = list(reversed(
        images_sorted[:(rows * columns_worst)] + \
        images_sorted[-(rows * columns_best):]
    ))

    for c in range(columns_best + columns_worst):
        for r in range(rows):
            n = c * rows + r

            if verbose:
                print("running prediction for image {}".format(n + 1))

            path = images_show[n]
            img = imread(path)

            axes[r, 2 * c].imshow(img)
            axes[r, 2 * c + 1].imshow(PredictColor(model)(img))

            axes[r, 2 * c].set_ylabel("{}%".format(round(100 * results[path])))

    # plot divider
    plt.tight_layout()

    _subplot_divider(fig, axes, 'vertical', 2 * columns_best - 1)

    # add titles
    for i, ax in enumerate(axes[0, :]):
        if i % 2 == 0:
            ax.set_title("Ground Truth")
        else:
            ax.set_title("Ours")

    fmt = '{}' + ' ' * 140 + '{}'

    suptitle = fmt.format(r'Fooled more often $\longleftarrow$',
                          r'$\longrightarrow$ Fooled less often')

    fig.suptitle(suptitle, y=0)


def classification_performance_demo(model,
                                    image_dir,
                                    transform=None,
                                    device='cuda',
                                    verbose=False):

    # create dataloader
    tr = [
        transforms.ToPILImage(),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])
    ]

    if transform is not None:
        tr = [transform] + tr

    dataloader = _dataloader(image_dir, transform=transforms.Compose(tr))

    # create classification model
    model = model.to(device)

    model.eval()

    # run prediction
    with torch.no_grad():
        correct = 0
        for i, (img, label) in enumerate(dataloader):
            img = img.to(device)

            if verbose:
                _display_progress(i, len(dataloader))

            if model(img).argmax().item() == label:
                correct += 1

    return correct / len(dataloader)


def raw_accuracy_demo(model, image_dir, reweigh_classes=False, verbose=False):
    dataloader = _dataloader(image_dir, labeled=False)

    predict_color = PredictColor(model, output_lab=True)

    auc_total = 0

    for i, img in enumerate(dataloader):
        if verbose:
            _display_progress(i, len(dataloader))

        img_rgb = _torch_to_numpy(img)
        img_lab = rgb_to_lab(img_rgb)

        img_pred = predict_color(img_rgb)

        auc_total += _auc(img_lab[:, :, 1:],
                          img_pred[:, :, 1:],
                          reweigh_classes=reweigh_classes)

    return auc_total / len(dataloader)
