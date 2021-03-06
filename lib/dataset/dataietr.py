


import os
import random
import cv2
import numpy as np
import traceback

from lib.helper.logger import logger
from tensorpack.dataflow import DataFromGenerator
from tensorpack.dataflow import BatchData, MultiProcessPrefetchData


from lib.dataset.centernet_data_sampler import get_affine_transform,affine_transform

from lib.dataset.augmentor.augmentation import Random_scale_withbbox,\
                                                Random_flip,\
                                                baidu_aug,\
                                                dsfd_aug,\
                                                Fill_img,\
                                                Rotate_with_box,\
                                                produce_heatmaps_with_bbox
from lib.dataset.augmentor.data_aug.bbox_util import *
from lib.dataset.augmentor.data_aug.data_aug import *
from lib.dataset.augmentor.visual_augmentation import ColorDistort,pixel_jitter

from lib.dataset.centernet_data_sampler import produce_heatmaps_with_bbox_official,affine_transform
from train_config import config as cfg


import math
class data_info():
    def __init__(self,img_root,txt):
        self.txt_file=txt
        self.root_path = img_root
        self.metas=[]


        self.read_txt()

    def read_txt(self):
        with open(self.txt_file) as _f:
            txt_lines=_f.readlines()
        txt_lines.sort()
        for line in txt_lines:
            line=line.rstrip()

            _img_path=line.rsplit('| ',1)[0]
            _label=line.rsplit('| ',1)[-1]

            current_img_path=os.path.join(self.root_path,_img_path)
            current_img_label=_label
            self.metas.append([current_img_path,current_img_label])

            ###some change can be made here
        logger.info('the dataset contains %d images'%(len(txt_lines)))
        logger.info('the datasets contains %d samples'%(len(self.metas)))


    def get_all_sample(self):
        random.shuffle(self.metas)
        return self.metas

class MutiScaleBatcher(BatchData):

    def __init__(self, ds, batch_size, remainder=False, use_list=False,scale_range=None,input_size=(512,512),divide_size=32):
        """
        Args:
            ds (DataFlow): A dataflow that produces either list or dict.
                When ``use_list=False``, the components of ``ds``
                must be either scalars or :class:`np.ndarray`, and have to be consistent in shapes.
            batch_size(int): batch size
            remainder (bool): When the remaining datapoints in ``ds`` is not
                enough to form a batch, whether or not to also produce the remaining
                data as a smaller batch.
                If set to False, all produced datapoints are guaranteed to have the same batch size.
                If set to True, `len(ds)` must be accurate.
            use_list (bool): if True, each component will contain a list
                of datapoints instead of an numpy array of an extra dimension.
        """
        super(BatchData, self).__init__(ds)
        if not remainder:
            try:
                assert batch_size <= len(ds)
            except NotImplementedError:
                pass

        self.batch_size = int(batch_size)
        self.remainder = remainder
        self.use_list = use_list

        self.scale_range=scale_range
        self.divide_size=divide_size

        self.input_size=input_size

    def __iter__(self):
        """
        Yields:
            Batched data by stacking each component on an extra 0th dimension.
        """

        ##### pick a scale and shape aligment

        holder = []
        for data in self.ds:

            image,boxes_,klass_=data[0],data[1],data[2]



            ###cove the small faces
            boxes_clean = []
            for i in range(boxes_.shape[0]):
                box = boxes_[i]

                if (box[3] - box[1]) < cfg.DATA.cover_small_face or (box[2] - box[0]) < cfg.DATA.cover_small_face:
                    image[int(box[1]):int(box[3]), int(box[0]):int(box[2]), :] =0
                    klass_[i]=0

            boxes_=np.array(boxes_)


            data=[image,boxes_,klass_]
            holder.append(data)

            if len(holder) == self.batch_size:
                target = self.produce_target(holder)

                yield BatchData.aggregate_batch(target, self.use_list)
                del holder[:]

        if self.remainder and len(holder) > 0:
            yield BatchData._aggregate_batch(holder, self.use_list)



    def produce_target(self,holder):
        alig_data = []

        if self.scale_range is not None:
            max_shape = [random.randint(*self.scale_range),random.randint(*self.scale_range)]

            max_shape[0] = int(np.ceil(max_shape[0] / self.divide_size) * self.divide_size)
            max_shape[1] = int(np.ceil(max_shape[1] / self.divide_size) * self.divide_size)

        else:
            max_shape=self.input_size

        # copy images to the upper left part of the image batch object
        for [image, boxes_, klass_] in holder:



            ### we do in map_function
            # image,boxes_=self.align_resize(image,boxes_,target_height=max_shape[0],target_width=max_shape[1])
            #
            # # construct an image batch object
            # image, shift_x, shift_y = self.place_image(image, target_height=max_shape[0], target_width=max_shape[1])
            # boxes_[:, 0:4] = boxes_[:, 0:4] + np.array([shift_x, shift_y, shift_x, shift_y], dtype='float32')
            #image = image.astype(np.uint8)


            if cfg.TRAIN.vis:
                for __box in boxes_:
                    cv2.rectangle(image, (int(__box[0]), int(__box[1])),
                                  (int(__box[2]), int(__box[3])), (255, 0, 0), 4)

            heatmap, wh,reg,ind,reg_mask = self.produce_for_centernet(image,boxes_, klass_)

            if cfg.DATA.channel==1:
                image=cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
                image=np.expand_dims(image,-1)

            alig_data.append([image,heatmap, wh,reg,ind,reg_mask])

        return alig_data



    def place_image(self,img_raw,target_height,target_width):



        channel = img_raw.shape[2]
        raw_height = img_raw.shape[0]
        raw_width = img_raw.shape[1]



        start_h=random.randint(0,target_height-raw_height)
        start_w=random.randint(0,target_width-raw_width)

        img_fill = np.zeros([target_height,target_width,channel], dtype=img_raw.dtype)
        img_fill[start_h:start_h+raw_height,start_w:start_w+raw_width]=img_raw

        return img_fill,start_w,start_h

    def align_resize(self,img_raw,boxes,target_height,target_width):
        ###sometimes use in objs detects
        h, w, c = img_raw.shape


        scale_y = target_height / h
        scale_x = target_width / w

        scale = min(scale_x, scale_y)

        image = cv2.resize(img_raw, None, fx=scale, fy=scale)
        boxes[:,:4]=boxes[:,:4]*scale

        return image, boxes


    def produce_for_centernet(self,image,boxes,klass,num_klass=cfg.DATA.num_class):
        # hm,reg_hm=produce_heatmaps_with_bbox(image,boxes,klass,num_klass)
        heatmap, wh,reg,ind,reg_mask = produce_heatmaps_with_bbox_official(image, boxes, klass, num_klass)
        return heatmap, wh,reg,ind,reg_mask


    def make_safe_box(self,image,boxes):
        h,w,c=image.shape

        boxes[boxes[:,0]<0]=0
        boxes[boxes[:, 1] < 0] = 0
        boxes[boxes[:, 2] >w] = w-1
        boxes[boxes[:, 3] >h] = h-1
        return boxes





class DsfdDataIter():

    def __init__(self, img_root_path='', ann_file=None, training_flag=True, shuffle=True):

        self.color_augmentor = ColorDistort()

        self.training_flag = training_flag

        self.lst = self.parse_file(img_root_path, ann_file)

        self.shuffle = shuffle

        self.space_augmentor = Sequence([RandomShear()])
    def __iter__(self):
        idxs = np.arange(len(self.lst))

        while True:
            if self.shuffle:
                np.random.shuffle(idxs)
            for k in idxs:
                yield self._map_func(self.lst[k], self.training_flag)

    def __len__(self):
        return len(self.lst)

    def parse_file(self,im_root_path,ann_file):
        '''
        :return: [fname,lbel]     type:list
        '''
        logger.info("[x] Get dataset from {}".format(im_root_path))

        ann_info = data_info(im_root_path, ann_file)
        all_samples = ann_info.get_all_sample()

        return all_samples

    def _map_func_raw(self,dp,is_training):
        """Data augmentation function."""
        ####customed here
        try:
            fname, annos = dp
            image = cv2.imread(fname, cv2.IMREAD_COLOR)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            labels = annos.split(' ')
            boxes = []


            for label in labels:
                bbox = np.array(label.split(','), dtype=np.float)
                boxes.append([bbox[0], bbox[1], bbox[2], bbox[3], bbox[4]])

            boxes = np.array(boxes, dtype=np.float)


            if is_training:

                sample_dice = random.uniform(0, 1)
                if sample_dice <= 0.3:

                    ###随机crop
                    image, boxes = Random_scale_withbbox(image, boxes, target_shape=[cfg.DATA.hin, cfg.DATA.win],
                                                         jitter=0.5)
                    #image, boxes =self.space_augmentor(image.copy(),boxes.copy())

                else:
                    ### 不crop
                    image = image.astype(np.uint8)
                    boxes = boxes


                if random.uniform(0, 1) > 0.5:
                    image, boxes = Random_flip(image, boxes)

                # if random.uniform(0, 1) > 0.5:
                #     boxes_ = boxes[:, 0:4]
                #     klass_ = boxes[:, 4:]
                #     angel=random.uniform(-5,5)
                #     image, boxes_ = Rotate_with_box(image,angel, boxes_)
                #     boxes = np.concatenate([boxes_, klass_], axis=1)


                if random.uniform(0, 1) > 0.5:
                    image =self.color_augmentor(image)
                # if random.uniform(0, 1) > 0.5:
                #     image =pixel_jitter(image,15)

            else:
                boxes_ = boxes[:, 0:4]
                klass_ = boxes[:, 4:]
                image, shift_x, shift_y = Fill_img(image, target_width=cfg.DATA.win, target_height=cfg.DATA.hin)
                boxes_[:, 0:4] = boxes_[:, 0:4] + np.array([shift_x, shift_y, shift_x, shift_y], dtype='float32')
                h, w, _ = image.shape
                boxes_[:, 0] /= w
                boxes_[:, 1] /= h
                boxes_[:, 2] /= w
                boxes_[:, 3] /= h
                image = image.astype(np.uint8)
                image = cv2.resize(image, (cfg.DATA.win, cfg.DATA.hin))

                boxes_[:, 0] *= cfg.DATA.win
                boxes_[:, 1] *= cfg.DATA.hin
                boxes_[:, 2] *= cfg.DATA.win
                boxes_[:, 3] *= cfg.DATA.hin
                image = image.astype(np.uint8)
                boxes = np.concatenate([boxes_, klass_], axis=1)




            if boxes.shape[0] == 0 or np.sum(image) == 0:
                boxes_ = np.array([[0, 0, 100, 100]])
                klass_ = np.array([0])
            else:
                boxes_ = np.array(boxes[:, 0:4], dtype=np.float32)
                klass_ = np.array(boxes[:, 4], dtype=np.int64)




        except:
            logger.warn('there is an err with %s' % fname)
            traceback.print_exc()
            image = np.zeros(shape=(cfg.DATA.hin, cfg.DATA.win, 3), dtype=np.float32)
            boxes_ = np.array([[0, 0, 100, 100]])
            klass_ = np.array([0])


        return image, boxes_, klass_
    def _map_func(self,dp,is_training):
        """Data augmentation function."""
        ####customed here
        try:
            fname, annos = dp
            image = cv2.imread(fname, cv2.IMREAD_COLOR)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            labels = annos.split(' ')
            boxes = []


            for label in labels:
                bbox = np.array(label.split(','), dtype=np.float)
                boxes.append([bbox[0], bbox[1], bbox[2], bbox[3], bbox[4]])

            boxes = np.array(boxes, dtype=np.float)

            img=image

            if is_training:

                height, width = img.shape[0], img.shape[1]
                c = np.array([img.shape[1] / 2., img.shape[0] / 2.], dtype=np.float32)
                if 0:
                    input_h = (height | self.opt.pad) + 1
                    input_w = (width | self.opt.pad) + 1
                    s = np.array([input_w, input_h], dtype=np.float32)
                else:
                    s = max(img.shape[0], img.shape[1]) * 1.0
                    input_h, input_w = cfg.DATA.hin, cfg.DATA.win



                flipped=False
                if 1:
                    if 1:
                        s = s * np.random.choice(np.arange(0.6, 1.4, 0.1))
                        w_border = self._get_border(128, img.shape[1])
                        h_border = self._get_border(128, img.shape[0])
                        c[0] = np.random.randint(low=w_border, high=img.shape[1] - w_border)
                        c[1] = np.random.randint(low=h_border, high=img.shape[0] - h_border)

                    if np.random.random() < 0.5:
                        flipped=True
                        img = img[:, ::-1, :]
                        c[0] = width - c[0] - 1


                trans_output = get_affine_transform(c, s, 0, [input_w, input_h])


                inp = cv2.warpAffine(img, trans_output,
                                     (input_w, input_h),
                                     flags=cv2.INTER_LINEAR)

                boxes_ = boxes[:,:4]
                klass_ = boxes[:,4:5]


                boxes_refine=[]
                for k in range(boxes_.shape[0]):
                    bbox = boxes_[k]

                    cls_id = klass_[k]
                    if flipped:
                        bbox[[0, 2]] = width - bbox[[2, 0]] - 1
                    bbox[:2] = affine_transform(bbox[:2], trans_output)
                    bbox[2:] = affine_transform(bbox[2:], trans_output)
                    bbox[[0, 2]] = np.clip(bbox[[0, 2]], 0, input_w - 1)
                    bbox[[1, 3]] = np.clip(bbox[[1, 3]], 0, input_h - 1)

                    boxes_refine.append(bbox)

                boxes_refine=np.array(boxes_refine)

                image = inp.astype(np.uint8)

                if random.uniform(0, 1) > 0.5:
                    image =self.color_augmentor(image)
                # if random.uniform(0, 1) > 0.5:
                #     image =pixel_jitter(image,15)
                image = image.astype(np.uint8)

                boxes = np.concatenate([boxes_refine, klass_], axis=1)
            else:
                boxes_ = boxes[:, 0:4]
                klass_ = boxes[:, 4:]
                image, shift_x, shift_y = Fill_img(image, target_width=cfg.DATA.win, target_height=cfg.DATA.hin)
                boxes_[:, 0:4] = boxes_[:, 0:4] + np.array([shift_x, shift_y, shift_x, shift_y], dtype='float32')
                h, w, _ = image.shape
                boxes_[:, 0] /= w
                boxes_[:, 1] /= h
                boxes_[:, 2] /= w
                boxes_[:, 3] /= h
                image = image.astype(np.uint8)
                image = cv2.resize(image, (cfg.DATA.win, cfg.DATA.hin))

                boxes_[:, 0] *= cfg.DATA.win
                boxes_[:, 1] *= cfg.DATA.hin
                boxes_[:, 2] *= cfg.DATA.win
                boxes_[:, 3] *= cfg.DATA.hin
                image = image.astype(np.uint8)
                boxes = np.concatenate([boxes_, klass_], axis=1)




            if boxes.shape[0] == 0 or np.sum(image) == 0:
                boxes_ = np.array([[0, 0, 100, 100]])
                klass_ = np.array([0])
            else:
                boxes_ = np.array(boxes[:, 0:4], dtype=np.float32)
                klass_ = np.array(boxes[:, 4], dtype=np.int64)




        except:
            logger.warn('there is an err with %s' % fname)
            traceback.print_exc()
            image = np.zeros(shape=(cfg.DATA.hin, cfg.DATA.win, 3), dtype=np.float32)
            boxes_ = np.array([[0, 0, 100, 100]])
            klass_ = np.array([0])


        return image, boxes_, klass_

    def _get_border(self, border, size):
        i = 1
        while size - border // i <= border // i:
            i *= 2
        return border // i



class DataIter():
    def __init__(self, img_root_path='', ann_file=None, training_flag=True):

        self.shuffle = True
        self.training_flag = training_flag

        self.num_gpu = cfg.TRAIN.num_gpu
        self.batch_size = cfg.TRAIN.batch_size
        self.process_num = cfg.TRAIN.process_num
        self.prefetch_size = cfg.TRAIN.prefetch_size

        self.generator = DsfdDataIter(img_root_path, ann_file, self.training_flag )

        self.ds = self.build_iter()



    def parse_file(self, im_root_path, ann_file):

        raise NotImplementedError("you need implemented the parse func for your data")

    def build_iter(self,):


        ds = DataFromGenerator(self.generator)


        if cfg.DATA.mutiscale and self.training_flag:
            ds = MutiScaleBatcher(ds, self.num_gpu * self.batch_size, scale_range=cfg.DATA.scales,
                                  input_size=(cfg.DATA.hin, cfg.DATA.win))
        else:
            ds = MutiScaleBatcher(ds, self.num_gpu * self.batch_size, input_size=(cfg.DATA.hin, cfg.DATA.win))

        ds = MultiProcessPrefetchData(ds, self.prefetch_size, self.process_num)
        ds.reset_state()
        ds = ds.get_data()
        return ds


    def __next__(self):
        return next(self.ds)

