import numpy as np
from utils.utils_bbox import batch_iou, xy2wh, wh2xy, center2wh, wh2center


def check_fits(box, adjust=True, jitter=False):
    # checks if the box fits in an image and shifts the box to fit, if possible
    good = np.max(box[2:]) < 1
    xbox = wh2xy(box)
    if good and adjust:
        tl_ind = xbox < 0
        br_ind = xbox >= 1
        delta = (np.zeros(4) - xbox) * tl_ind + (xbox - np.ones(4)) * br_ind
        if jitter and np.random.rand() > 0.7:
            delta *= np.random.uniform(0.5, 1)
        xbox = np.clip(xbox + delta, 0, 0.999)
    return good, xy2wh(xbox)


class InstanceSampler():
    """Creates and manages contextual images generated by bounding boxes"""

    def __init__(self, loader=None, exclude=[], random_box=False,
                 neg_bias=1, n_neighborhoods=1):
        """Uses voc_loader to define context images manager

        Either feed parameters of a loader wanted or the loader itself
        """

        self.loader = loader
        self.max_sample_tries = 50
        self.min_context_area = 0.3
        self.n_neighborhoods = n_neighborhoods
        self.num_classes = self.loader.num_classes
        self.neg_bias = neg_bias
        self.neg_prob = (1 / self.num_classes if self.neg_bias == 1
                         else self.neg_bias / (1 + self.neg_bias))

        self.create_pos_db()
        self.draw_bbox = (self.draw_random_bbox if random_box
                          else self.draw_distro_bbox)

    def create_pos_db(self):
        """Creates a database of positive bounding boxes"""
        self.pos_names, self.bboxes, self.labels, self.whs = [], [], [], []
        for name in self.loader.get_filenames('pos'):
            bboxes, _, cats, w, h, diff = self.loader.read_annotations(name)
            for i in range(len(cats)):
                box = bboxes[i]
                if np.prod(box[2:]) / (w * h) < 1 - self.min_context_area:
                    self.bboxes.append(box)
                    self.pos_names.append(name)
                    self.labels.append(cats[i])
                    self.whs.append(np.array([w, h]))
        print('Created positive database of %d samples' % len(self.labels))

    def get_bbox_distribution(self):
        """Builds a histogram2d distribution of scale and aspect ratio

        Uses positive bounding boxes of the training set to do this
        """

        try:
            return self.distro
        except AttributeError:
            print('Initializing the distribution')
            all_ars, all_scales = [], []
            for i in range(len(self.pos_names)):
                all_scales.append(self.bboxes[i][2:].prod()
                                  / self.whs[i].prod())
                all_ars.append(self.bboxes[i][2] / self.bboxes[i][3])
            bins = 10
            freq, scale_edges, ars_edges = np.histogram2d(np.array(all_scales),
                                                          np.array(all_ars),
                                                          bins=bins)
            grid = np.stack(np.meshgrid(scale_edges, ars_edges),
                            axis=-1)[:bins, :bins, :]
            freq = freq + 0.05
            scale_bin = scale_edges[1] - scale_edges[0]
            ar_bin = ars_edges[1] - ars_edges[0]
            self.distro = (grid.reshape(-1, 2), freq.reshape(-1),
                           scale_bin, ar_bin)
            return self.distro

    def draw_distro_bbox(self, w, h):
        """Draws a box from the estimated distribution"""

        grid, freqs, scale_bin, ar_bin = self.get_bbox_distribution()
        while True:
            try:
                bin = np.random.choice(np.arange(len(freqs)), p=freqs / freqs.sum())
                scale, ar = grid[bin]
                scale = np.random.uniform(scale, scale + scale_bin) * w * h
                ar = np.random.uniform(ar, ar + ar_bin)
                wi = int(np.sqrt(ar * scale))
                hi = int(np.sqrt(scale / ar))
                xmin = np.random.randint(0, w - wi - 1)
                ymin = np.random.randint(0, h - hi - 1)
                break
            except ValueError:
                pass
        return np.array([xmin, ymin, wi, hi]).reshape((1, 4))

    def draw_random_bbox(self, w, h, gap=10):
        xmin = np.random.randint(0, w - gap - 1)
        ymin = np.random.randint(0, h - gap - 1)
        xmax = np.random.randint(xmin + gap, w)
        ymax = np.random.randint(ymin + gap, h)
        return np.array([xmin, ymin, xmax - xmin, ymax - ymin]).reshape((1, 4))

    def find_frame(self, box, distort_bbox=True):
        """Finds the boarders of contextual neighborhood for a given box

        Given a bounding box, finds an enclosing box (with some constraints) in
        order to crop this box out of an image to make a contextual image
        """

        # takes a normed in [0, 1] bbox as input
        box = box.clip(0, 0.99)

        # distorts the bounding box
        if distort_bbox:
            for i in range(20):
                scale_up = np.random.uniform(1, 1.3)
                cbox = wh2center(box)
                cbox[2:] = cbox[2:] * scale_up
                drift_bounds = cbox[2:] * (scale_up - 1) / 2
                x_drift = np.random.uniform(-drift_bounds[0], drift_bounds[0])
                y_drift = np.random.uniform(-drift_bounds[1], drift_bounds[1])
                cbox[:2] += np.array([x_drift, y_drift])
                new_box = center2wh(cbox)
                good, new_box = check_fits(new_box, jitter=True)
                _w, _h = new_box[2:]
                enough_space_left = 1 - _w * _h > self.min_context_area
                if good and enough_space_left:
                    box = new_box
                    break

        x, y, w, h = box
        # sampling w, h parameters of a frame
        min_area = self.min_context_area + w*h    # since the box will be cut out
        fw = np.random.uniform(max(w, min_area), 1)
        fh = np.random.uniform(max(h, min_area / fw), 1)

        # sampling x, y parameters of a frame
        img_size_constr_right = np.minimum(1 - np.array([fw, fh]), np.array([x, y]))
        obj_box_constr_left = np.array([x + w - fw, y + h - fh]).clip(0, 1)
        fx = np.random.uniform(obj_box_constr_left[0], img_size_constr_right[0])
        fy = np.random.uniform(obj_box_constr_left[1], img_size_constr_right[1])
        return box, np.array([fx, fy, fw, fh])

    def sample_negative(self, name=None):
        """Samples a background contextual image"""

        cat = None
        while cat is None:
            name = np.random.choice(self.loader.filenames) if name is None else name
            bboxes, _, cats, w, h, diff = self.loader.read_annotations(name)
            image = self.loader.load_image(name)
            if len(cats) == 0:
                bbox = self.draw_bbox(w, h)
                cat = 0
            else:
                for i in range(self.max_sample_tries):
                    bbox = self.draw_bbox(w, h)
                    ious = batch_iou(bboxes, bbox)
                    area = bbox[0, 2] * bbox[0, 3]
                    areas = bboxes[:, 2] * bboxes[:, 3]
                    inter_fraq = (ious * (areas + area) / (1 + ious)) / areas
                    if np.max(inter_fraq) < 0.3:
                        cat = 0
                        break
        self.name = name
        return [image, bbox, cat, w, h]

    def sample_positive(self, name=None):
        """Samples a positive contextual image"""

        select_inds = np.arange(len(self.labels))
        if name is not None:
            select_inds = select_inds[[name in n for n in self.pos_names]]
        ind = np.random.choice(select_inds)
        image = self.loader.load_image(self.pos_names[ind])
        self.name = self.pos_names[ind]
        return [image, self.bboxes[ind], self.labels[ind],
                self.whs[ind][0], self.whs[ind][1]]

    def rnd_sample(self, name=None):
        if np.random.rand() > self.neg_prob:
            return self.sample_positive(name)
        else:
            return self.sample_negative(name)

    def get_sample(self, given_name=None):
        """Get a training sample that goes to the training pipeline"""

        keys = ['img', 'bbox', 'label', 'w', 'h', 'frame']

        if given_name:
            if given_name in self.pos_names:
                sample = self.rnd_sample(given_name)
            else:
                sample = self.sample_negative(given_name)
        else:
            sample = self.rnd_sample()

        # if np.random.rand() > neg_prob:
        #     if given_name:
        #         if given_name in self.pos_names:
        #             sample = self.sample_positive(given_name)
        #     else:
        #         sample = self.sample_positive()
        # else:
        #     sample = self.sample_negative(given_name)
        w, h = sample[-2:]
        sample[1] = np.squeeze(np.clip(np.array(sample[1]) / np.array([w, h, w, h]), 0, 0.999))
        sample[1], frame = self.find_frame(sample[1])
        sample.append(frame)
        out = {keys[i]: sample[i] for i in range(len(keys))}
        return out

    def get_test_sample(self, name, n_candidates=200):
        """Get a sample used for inference

        Args:
            n_candidates (int): the number of contextual images to be
        constructed for an original image to be augmented
        """

        keys = ['img', 'bboxes', 'frames', 'w', 'h']
        image = self.loader.load_image(name)
        gt_bboxes, _, gt_cats, w, h, diff = self.loader.read_annotations(name)
        size = np.array([w, h, w, h])
        candidates, frames = [], []
        for i in range(n_candidates // self.n_neighborhoods):
            cand_box = self.draw_bbox(w, h)
            cand = np.squeeze(np.clip(np.array(cand_box) / size, 0, 0.999))
            for neib in range(self.n_neighborhoods):
                cand, frame = self.find_frame(
                    cand, distort_bbox=(neib == 0))
                candidates.append(cand)
                frames.append(frame)
        sample = [image, np.array(candidates), np.array(frames), w, h]

        out = {keys[i]: sample[i] for i in range(len(keys))}
        return out
