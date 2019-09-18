from __future__ import division
import os
import time
from glob import glob
import cv2
import scipy.ndimage
from models import *
from utils import *
from seg_eval import *
import tensorflow as tf
import numpy as np
import copy
import nibabel as nib
from imgaug import augmenters as iaa


class CascadedModel(object):
    """ Implementation of 3D U-net"""

    def __init__(self, sess, param_set):
        self.sess = sess
        self.para_set = param_set
        self.phase = param_set['phase']
        self.batch_size = param_set['batch_size']
        self.inputI_size = param_set['inputI_size']
        self.inputI_chn = param_set['inputI_chn']
        self.outputI_size = param_set['outputI_size']
        self.output_chn = param_set['output_chn']
        self.resize_r = param_set['resize_r']
        self.traindata_dir = param_set['traindata_dir']
        self.chkpoint_dir = param_set['chkpoint_dir']
        self.lr = param_set['learning_rate']
        self.beta1 = param_set['beta1']
        self.epoch = param_set['epoch']
        self.model_name = param_set['model_name']
        self.save_intval = param_set['save_intval']
        self.testdata_dir = param_set['testdata_dir']
        self.labeling_dir = param_set['labeling_dir']
        self.ovlp_ita = param_set['ovlp_ita']
        self.step = param_set['step']
        self.rename_map = param_set['rename_map']
        self.rename_map = [int(s) for s in self.rename_map.split(',')]
        self.Blocks = param_set['Blocks']
        self.Columns = param_set['Columns']
        self.Stages = param_set['Stages']
        self.fg_ratio = param_set['fg_ratio']
        self.bg_ratio = param_set['bg_ratio']
        self.save_config = True
        self.focal_loss_flag = param_set['focal_loss_flag']
        # build model graph
        self.build_cascade_model()

    # dice loss function
    def dice_loss_fun(self, pred, input_gt):
        input_gt = tf.one_hot(input_gt, self.output_chn)
        #############################################################
        # softmaxpred = tf.nn.softmax(pred)
        # input_gt = produce_mask_background(input_gt,softmaxpred )
        ####################################################################
        dice = 0
        for i in range(self.output_chn):
            inse = tf.reduce_mean(
                pred[:, :, :, :, i] * input_gt[:, :, :, :, i])
            l = tf.reduce_sum(pred[:, :, :, :, i] * pred[:, :, :, :, i])
            r = tf.reduce_sum(input_gt[:, :, :, :, i]
                              * input_gt[:, :, :, :, i])
            dice = dice + 2 * inse / (l + r)
        return -dice

    # generalized_dice loss function
    def generalize_dice_loss(self, pred, input_gt):
        gt_onehot = tf.one_hot(input_gt, self.output_chn)
        denominator = 0
        nominator = 0
        for j in range(self.output_chn):
            # predj = tf.clip_by_value(pred[:,:,:,:,j], 0.0005, 1)
            predj = pred[:, :, :, :, j]
            intesection = tf.reduce_sum(predj * gt_onehot[:, :, :, :, j])
            r = tf.reduce_sum(gt_onehot[:, :, :, :, j]
                              * gt_onehot[:, :, :, :, j])
            p = tf.reduce_sum(predj * predj)
            union = r + p
            wj = 1 / (r * r + 0.000001)
            denominator += wj * union
            nominator += wj * intesection
        dice = 1 - 2 * nominator / (denominator + 0.000001)
        return dice

    # class-weighted cross-entropy loss function
    def softmax_weighted_loss(self, logits, labels):
        """
        Loss = weighted * -target*log(softmax(logits))
        :param logits: probability score
        :param labels: ground_truth
        :return: softmax-weifhted loss
        """
        gt = tf.one_hot(labels, self.output_chn)
        pred = logits
        softmaxpred = tf.nn.softmax(pred)
        #############################################################
        # gt = produce_mask_background(gt, softmaxpred, self.fg_ratio,
        # self.bg_ratio)  # produce mask for the GT
        gt = tf.stop_gradient(gt)  # stop gradient to the GT
        ####################################################################
        loss = 0
        for i in range(self.output_chn):
            gti = gt[:, :, :, :, i]
            predi = softmaxpred[:, :, :, :, i]
            weighted = 1 - (tf.reduce_sum(gti) / tf.reduce_sum(gt))
            # test whether to use focal_loss
            if self.focal_loss_flag:
                focal_loss = tf.pow(
                    (1 - tf.clip_by_value(predi, 0.005, 1)), 4, name=None)
            else:
                focal_loss = 1
            loss = loss + - \
                tf.reduce_mean(weighted * gti * focal_loss * tf.log(tf.clip_by_value(predi, 0.005, 1)))
        return loss

    # sigmoid focal loss
    def sigmoid_focal_loss(self, logits, labels, gamma, alpha):
        # no need for one-hot encoding
        if self.focal_loss_flag:
            p = tf.clip_by_value(tf.nn.sigmoid(logits), 0.005, 1)
            term1 = (1 - p) ** gamma * tf.log(p)
            term2 = p ** gamma * tf.log(1 - p)
            loss_arr = tf.to_float((labels == 1)) * alpha * term1 + \
                tf.to_float((labels != 1)) * (1 - alpha) * term2
            loss = tf.reduce_sum(-loss_arr)
        else:
            p = tf.clip_by_value(tf.nn.sigmoid(logits), 0.005, 1)
            loss_arr = tf.to_float(labels == 1) * tf.log(p) + \
                tf.to_float((labels != 1)) * tf.log(1 - p)
            loss = tf.reduce_sum(-loss_arr)
        return loss

    # build cascade graph
    def build_cascade_model(self):
        # There exits three stages ,each stage for a specific class
        # stage1  3D-unet for the whole tumor
        self.stage1_inputI = tf.placeholder(
            dtype=tf.float32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_chn],
            name='stage1_inputI')
        self.stage1_input_gt = tf.placeholder(
            dtype=tf.int32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size],
            name='stage1_input_gt')
        print("stage1 Input image：", self.stage1_inputI)
        print("stage1 Input label:", self.stage1_input_gt)

        self.stage1_pred_prob, self.stage1_pred_label, self.stage1_aux0_prob, \
        self.stage1_aux1_prob, self.stage1_aux2_prob = unet(
            self.stage1_inputI, self.output_chn)

        # stage2 3D-Unet for the tumor core(TC)
        self.stage2_inputI = tf.placeholder(
            dtype=tf.float32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size,
                1])
        self.stage2_input_gt = tf.placeholder(
            dtype=tf.int32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size])
        self.stage2_pred_prob, self.stage2_pred_label = unet_resnet(
            self.stage1_pred_prob, self.stage2_inputI, self.output_chn, 'stage2')

        # stage3 3D-Unet for the Necrotic
        self.stage3_inputI = tf.placeholder(
            dtype=tf.float32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size,
                1])
        self.stage3_input_gt = tf.placeholder(
            dtype=tf.int32,
            shape=[
                self.batch_size,
                self.inputI_size,
                self.inputI_size,
                self.inputI_size])
        self.stage3_pred_prob, self.stage3_pred_label = unet_resnet(
            self.stage2_pred_prob, self.stage3_inputI, self.output_chn, 'stage3')

        # loss functions
        # stage1
        # ========= class-weighted cross-entropy loss
        self.main_wght_loss = self.softmax_weighted_loss(
            self.stage1_pred_prob, self.stage1_input_gt)
        self.aux0_wght_loss = self.softmax_weighted_loss(
            self.stage1_aux0_prob, self.stage1_input_gt)
        self.aux1_wght_loss = self.softmax_weighted_loss(
            self.stage1_aux1_prob, self.stage1_input_gt)
        self.aux2_wght_loss = self.softmax_weighted_loss(
            self.stage1_aux2_prob, self.stage1_input_gt)
        self.total_wght_loss = self.main_wght_loss + 0.3 * self.aux0_wght_loss + \
            0.6 * self.aux1_wght_loss + 0.9 * self.aux2_wght_loss

        # stage2
        self.stage2_loss = self.softmax_weighted_loss(
            self.stage2_pred_prob, self.stage2_input_gt)

        # stage3
        self.stage3_loss = self.softmax_weighted_loss(
            self.stage3_pred_prob, self.stage3_input_gt)

        # losses from the three stages(weighted)
        self.all_stages_loss = 0.4 * self.total_wght_loss + \
            0.8 * self.stage2_loss + self.stage3_loss

        # trainable variables returns the variables that can be trained.
        self.u_vars = tf.trainable_variables()

        # extract the layers for fine tuning
        ft_layer = ['conv1/kernel:0',
                    'conv2/kernel:0',
                    'conv3a/kernel:0',
                    'conv3b/kernel:0',
                    'conv4a/kernel:0',
                    'conv4b/kernel:0']

        self.ft_vars = []
        for var in self.u_vars:
            for k in range(len(ft_layer)):
                if ft_layer[k] in var.name:
                    self.ft_vars.append(var)  # 把这玩意作为变量名称放进去
                    break

        # create model saver
        self.saver = tf.train.Saver(max_to_keep=20)
        # saver to load pre-trained C3D model 设置新的saver
        self.saver_ft = tf.train.Saver(self.ft_vars)

    # train function
    def train(self):
        u_optimizer = tf.train.AdamOptimizer(
            learning_rate=self.lr,
            beta1=self.beta1).minimize(
            self.all_stages_loss,
            var_list=self.u_vars)
        # initialize the parameters of the network
        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)

        # initialize the pretrined weights of the 3D cascaded model
        self.initialize_finetune()

        # save .log
        self.log_writer = tf.summary.FileWriter("./logs", self.sess.graph)

        counter = 1
        if self.load_chkpoint(self.chkpoint_dir, self.step):
            print(" [*] Load checkpoint succeed !")
        else:
            print(" [!] Failed to load the checkpoint...")

        # temporary file to save loss
        loss_log = open("loss.txt", "w")
        # lock the graph to be read-only in case we make any mistake when add nodes afterwards
        self.sess.graph.finalize()

        # data augment
        augmentation = iaa.SomeOf((1, 4), [
            iaa.Fliplr(0.5),
            iaa.Flipud(0.5),
            iaa.OneOf([iaa.Affine(rotate=90),
                       iaa.Affine(rotate=180),
                       iaa.Affine(rotate=270)]),
            iaa.Multiply((0.8, 1.5)),
            iaa.GaussianBlur(sigma=(0.0, 4.0)),
            iaa.Affine(rotate=(-45, 45))
        ])

        # Initilize the batch generator
        data_generator = BatchGenerator(
            batch_size=self.batch_size,
            shuffle=True,
            seed=1,
            volume_path=self.traindata_dir,
            modalities=self.inputI_chn,
            resize_r=self.resize_r,
            rename_map=self.rename_map,
            patch_dim=self.outputI_size,
            augmentation=augmentation)
        for epoch in np.arange(self.epoch):
            start_time = time.time()
            # Get the training data
            batch_img, batch_img2, batch_label, batch_label_stage2, batch_label_stage3 = next(
                data_generator)

            # Update Cascaded 3D U-net to get the loss of every step
            _, cur_train_loss = self.sess.run([u_optimizer, self.all_stages_loss],
                                              feed_dict={self.stage1_inputI: batch_img,
                                                         self.stage1_input_gt: batch_label,
                                                         self.stage2_inputI: batch_img2,
                                                         self.stage2_input_gt: batch_label_stage2,
                                                         self.stage3_inputI: batch_img2,
                                                         self.stage3_input_gt: batch_label_stage3})
            counter += 1
            if np.mod(epoch, 2) == 0:
                print(
                    "Epoch: [%2d] ：....time: %4.4f........................train_loss: %.8f" %
                    (epoch, time.time() - start_time, cur_train_loss))

            if np.mod(counter, self.save_intval) == 0:
                # validate and save checkpoints for several iterations
                # validate on both part of the training data and the validation data
                self.test(
                    counter=counter,
                    logname="train.log",
                    dataset="train_set",
                    save_pred=False,
                    save_log_single=False,
                    eval_flag=True)
                self.test(
                    counter=counter,
                    logname="test.log",
                    dataset="test_set",
                    save_pred=False,
                    save_log_single=False,
                    eval_flag=True)

                self.save_chkpoint(self.chkpoint_dir, self.model_name, counter)
        loss_log.close()


    def test(
            self,
            counter,
            logname,
            dataset,
            save_pred,
            save_log_single,
            eval_flag):

        test_log = open(logname, "a")
        draw_graph_log = open(dataset + "boxplot.log", "a")

        if self.save_config:
            # save configuration
            test_log.write("Configurations:\n")
            for (k, v) in self.para_set.items():
                test_log.write("{:30} {} \n".format(k, v))
                # test_log.write("\n")
            self.save_config = False
        else:
            pass
        # 初始化测试数据迭代器类
        # 判断测试训练集指标还是测试集指标
        if dataset == "train_set":
            volume_path = self.traindata_dir
        elif dataset == "test_set":
            volume_path = self.testdata_dir
        else:
            raise Exception("Test dataset not specified!")

        # Initilizing testing data generator Class
        # Attention, No augmentation while testing
        test_generator = BatchGenerator(
            batch_size=self.batch_size,
            shuffle=True,
            seed=1,
            volume_path=volume_path,
            modalities=self.inputI_chn,
            resize_r=self.resize_r,
            rename_map=self.rename_map,
            patch_dim=self.outputI_size,
            augmentation=None)

        # Instantiate the evaluation class to prepare for save slices of the predicted results and groundtruth.
        eval_class = Evaluation()

        # when test on the training dataset, we just use 50 examples
        if dataset == "train_set":
            test_file_paths = test_generator.file_list[0:50]
        elif dataset == "test_set":
            test_file_paths = test_generator.file_list
        else:
            raise Exception("Test dataset not specified!")

        all_dice_WT = np.zeros([len(test_file_paths), self.output_chn])
        all_sensentivity_WT = np.zeros([len(test_file_paths), self.output_chn])

        all_dice_TC = np.zeros([len(test_file_paths), self.output_chn])
        all_sensentivity_TC = np.zeros([len(test_file_paths), self.output_chn])

        all_dice_ET = np.zeros([len(test_file_paths), self.output_chn])
        all_sensentivity_ET = np.zeros([len(test_file_paths), self.output_chn])
        # test
        for i, file_path_dict in enumerate(test_file_paths):
            file_path = file_path_dict["path"]
            print(dataset, "Start Processing:", os.path.basename(file_path))
            vol_data, vol_data2, stage1_label, stage2_label, stage3_label,\
            vol_ori = test_generator.load_volumes_label(
                file_path, True)

            # if there exists grundtruth label
            if stage1_label == []:
                show_gt = False
            else:
                show_gt = True

            # reduce background region
            regions = get_brain_region(np.squeeze(vol_data2, axis=-1))
            vol_data_fg = vol_data[regions[0]:regions[1],
                                   regions[2]:regions[3], regions[4]:regions[5], :]
            vol_data2_fg = vol_data2[regions[0]:regions[1],
                                     regions[2]:regions[3], regions[4]:regions[5], :]
            if show_gt:
                stage1_label_fg = stage1_label[regions[0]:regions[1],
                                               regions[2]:regions[3], regions[4]:regions[5]]
                stage2_label_fg = stage2_label[regions[0]:regions[1],
                                               regions[2]:regions[3], regions[4]:regions[5]]
                stage3_label_fg = stage3_label[regions[0]:regions[1],
                                               regions[2]:regions[3], regions[4]:regions[5]]
            else:
                stage1_label_fg = stage1_label
                stage2_label_fg = stage2_label
                stage3_label_fg = stage3_label

            ref_affine = vol_ori.affine
            # ref_affine = np.eye(4)
            # data = vol_ori.get_data()

            resize_dim = (
                np.array(vol_data_fg.shape[0:3]) * self.resize_r).astype('int')
            # resize and normalization
            vol_data_fg = vol_data_fg.astype('float32')
            vol_data2_fg = vol_data2_fg.astype('float32')

            # vol_data_resized = resize(vol_data, resize_dim+(self.inputI_chn,), order=1, preserve_range=True)
            vol_data_resized = resize(
                vol_data_fg, resize_dim, order=1, preserve_range=True)
            vol_data2_resized = resize(
                vol_data2_fg, resize_dim, order=1, preserve_range=True)

            vol_data_norm = Preprocessing.Normalization(
                vol_data_resized.astype('float32'), axis=(0, 1, 2))
            vol_data2_norm = Preprocessing.Normalization(
                vol_data2_resized.astype("float32"))

            # Decopose volume data to several cubes ([batch_size, cube_size, cube_size, cube_size, modalities])
            cube_list = decompose_vol2cube_brain(
                vol_data_norm,
                self.inputI_size,
                self.inputI_chn,
                self.ovlp_ita)
            cube_list2 = decompose_vol2cube_brain(
                vol_data2_norm, self.inputI_size, 1, self.ovlp_ita)

            # get the prediction
            cube_label_list_WT = []
            cube_label_list_TC = []
            cube_label_list_ET = []
            for c in range(len(cube_list)):
                # 取出一个立方块 并且进行标准化(测试使用三个通道)
                cube2test = cube_list[c]
                cube2test_2 = cube_list2[c]

                if c % 20 == 0:
                    print("Predict %s MRI volumes %s cubes" % (i, c))
                # 获取单个立方块的预测结果
                # Get the prediction of a single cube
                self.sess.graph.finalize()
                cube_label_stage1, cube_label_stage2, cube_label_stage3 = self.sess.run(
                    [self.stage1_pred_label, self.stage2_pred_label, self.stage3_pred_label],
                    feed_dict={
                        self.stage1_inputI: cube2test,
                        self.stage2_inputI: cube2test_2,
                        self.stage3_inputI: cube2test_2,
                    })
                # sigmoid_pred =1 /(1+np.exp(-cube_prob))
                # cube_label_list.append(cube_label)
                cube_label_list_WT.append(cube_label_stage1)
                cube_label_list_TC.append(cube_label_stage2)
                cube_label_list_ET.append(cube_label_stage3)

            # Compose the results of those cubes
            composed_orig_WT = compose_label_cube2vol(
                cube_label_list_WT,
                resize_dim,
                self.inputI_size,
                self.ovlp_ita,
                self.output_chn)
            composed_orig_TC = compose_label_cube2vol(
                cube_label_list_TC,
                resize_dim,
                self.inputI_size,
                self.ovlp_ita,
                self.output_chn)
            composed_orig_NET = compose_label_cube2vol(
                cube_label_list_ET,
                resize_dim,
                self.inputI_size,
                self.ovlp_ita,
                self.output_chn)

            # WT_orig_size = resize(composed_orig_WT, vol_data.shape[0:3], order=0, preserve_range=True)
            # TC_orig_size = resize(composed_orig_TC, vol_data.shape[0:3], order=0, preserve_range=True)
            # NET_orig_size = resize(composed_orig_NET, vol_data.shape[0:3], order=0, preserve_range=True)

            WT_orig_size = composed_orig_WT
            TC_orig_size = composed_orig_TC
            NET_orig_size = composed_orig_NET

            WT_orig_size = WT_orig_size.astype('int16')
            TC_orig_size = TC_orig_size.astype('int16')
            NET_orig_size = NET_orig_size.astype('int16')

            # Attention, we use the NET to train the third stage, However, the final evaluation
            # should be  on the ET , so we transfer the prediction from NET to ET.
            ET_pre = copy.deepcopy(TC_orig_size)
            ET_pre[NET_orig_size == 1] = 0

            # postprocessing
            WT_orig_size = postprocessing(WT_orig_size)
            TC_orig_size = postprocessing(TC_orig_size)

            if eval_flag:
                ET_gt = copy.deepcopy(stage2_label_fg)
                ET_gt[stage3_label_fg == 1] = 0
                k_dice_c_WT = seg_eval_metric(
                    WT_orig_size, stage1_label_fg, self.output_chn)  # Calculate the dice score
                k_dice_c_TC = seg_eval_metric(
                    TC_orig_size, stage2_label_fg, self.output_chn)
                # k_dice_c_ET = seg_eval_metric(ET_orig_size, stage3_label, self.output_chn)
                k_dice_c_ET = seg_eval_metric(ET_pre, ET_gt, self.output_chn)

                k_dice_c_WT = np.round(k_dice_c_WT, decimals=3)
                k_dice_c_TC = np.round(k_dice_c_TC, decimals=3)
                k_dice_c_ET = np.round(k_dice_c_ET, decimals=3)

                all_dice_WT[i, :] = np.asarray(k_dice_c_WT)
                all_dice_TC[i, :] = np.asarray(k_dice_c_TC)
                all_dice_ET[i, :] = np.asarray(k_dice_c_ET)
                # print("dice_WT: {} dice_TC: {} dice_ET: {}".format(k_dice_c_WT[1], k_dice_c_TC[1], k_dice_c_ET[1]))
                print("[dice_WT, dice_TC, dice_ET]: {}".format(
                    [k_dice_c_WT[1], k_dice_c_TC[1], k_dice_c_ET[1]]))
                #############################################
                k_sensentivity_c_WT = sensitivity(
                    WT_orig_size, stage1_label_fg, self.output_chn)  # # Calculate the sensitivity score
                k_sensentivity_c_TC = sensitivity(
                    TC_orig_size, stage2_label_fg, self.output_chn)
                # k_sensentivity_c_ET = sensitivity(ET_orig_size, stage3_label, self.output_chn)
                k_sensentivity_c_ET = sensitivity(
                    ET_pre, ET_gt, self.output_chn)

                k_sensentivity_c_WT = np.round(k_sensentivity_c_WT, decimals=3)
                k_sensentivity_c_TC = np.round(k_sensentivity_c_TC, decimals=3)
                k_sensentivity_c_ET = np.round(k_sensentivity_c_ET, decimals=3)

                all_sensentivity_WT[i, :] = np.asarray(k_sensentivity_c_WT)
                all_sensentivity_TC[i, :] = np.asarray(k_sensentivity_c_TC)
                all_sensentivity_ET[i, :] = np.asarray(k_sensentivity_c_ET)

                print("[sensitivity_WT, sensitivity_TC, sensitivity_ET]: {}".format(
                    [k_sensentivity_c_WT[1], k_sensentivity_c_TC[1], k_sensentivity_c_ET[1]]))

                # write the evaluation result to the log file
                if save_log_single:
                    # test_log.write(
                    #     "{:30}WT:{} TC:{} ET:{} \n".format(os.path.basename(file_path), k_dice_c_WT, k_dice_c_TC, k_dice_c_ET))
                    # test_log.write(
                    #     "{:30} [WT, TC, ET]: {} \n".format(os.path.basename(file_path), [k_dice_c_WT[1], k_dice_c_TC[1],
                    # k_dice_c_ET[1]]))
                    draw_graph_log.write(
                        "{:30} {} {} {} {} {} {} {} {} {}\n".format(
                            os.path.basename(file_path),
                            k_dice_c_WT[1],
                            k_dice_c_TC[1],
                            k_dice_c_ET[1],
                            k_sensentivity_c_WT[1],
                            k_sensentivity_c_TC[1],
                            k_sensentivity_c_ET[1],
                            k_sensentivity_c_WT[0],
                            k_sensentivity_c_TC[0],
                            k_sensentivity_c_ET[0]))
            else:
                k_dice_c_WT = [0, 0]
                k_dice_c_TC = [0, 0]
                k_dice_c_ET = [0, 0]
            if save_pred:
                # Directories for displaying the prediction and gt mask slice by slice
                slice_path_WT = os.path.join(self.labeling_dir, "slice_WT")
                slice_path_TC = os.path.join(self.labeling_dir, "slice_TC")
                slice_path_NET = os.path.join(self.labeling_dir, "slice_NET")

                pre_vol_path = os.path.join(self.labeling_dir, "pre_volume")

                if not os.path.exists(pre_vol_path):
                    os.makedirs(pre_vol_path)
                # Test Flair
                volum_flair = vol_data[:, :, :, 0]
                volume_T1ce = np.squeeze(vol_data2, axis=-1)

                # restore bg region
                label_WT = np.zeros(vol_data2.shape[0:3], dtype="int")
                label_TC = np.zeros(vol_data2.shape[0:3], dtype="int")
                label_NET = np.zeros(vol_data2.shape[0:3], dtype="int")
                label_ET = np.zeros(vol_data2.shape[0:3], dtype="int")
                label_WT[regions[0]:regions[1], regions[2]:regions[3], regions[4]:regions[5]] = WT_orig_size
                label_TC[regions[0]:regions[1], regions[2]:regions[3], regions[4]:regions[5]] = TC_orig_size
                label_NET[regions[0]:regions[1], regions[2]:regions[3], regions[4]:regions[5]] = NET_orig_size
                label_ET[regions[0]:regions[1], regions[2]:regions[3], regions[4]:regions[5]] = ET_pre

                # Merge the labels with 3 binary mask.
                merged_pre_volume = self.merge_labels(
                    label_WT, label_TC, label_ET)

                # Remove small connected regions
                # reduce_small_cc = remove_minor_cc(merged_pre_volume, rej_ratio=0.3, rename_map=self.rename_map)
                reduce_small_cc = merged_pre_volume
                LGG_list = [
                    "BraTS19_TCIA09_225_1",
                    "BraTS19_TCIA09_248_1",
                    "BraTS19_TCIA09_381_1",
                    "BraTS19_TCIA10_127_1",
                    "BraTS19_TCIA10_195_1",
                    "BraTS19_TCIA10_232_1",
                    "BraTS19_TCIA10_236_1",
                    "BraTS19_TCIA10_609_1",
                    "BraTS19_TCIA10_614_1",
                    "BraTS19_TCIA10_631_1",
                    "BraTS19_TCIA12_146_1",
                    "BraTS19_TCIA12_613_1",
                    "BraTS19_TCIA12_641_1",
                    "BraTS19_TCIA13_626_1",
                    "BraTS19_TCIA13_646_1"]
                if os.path.basename(file_path) in LGG_list:
                    reduce_small_cc[reduce_small_cc == 4] = 1

                # Save the prediction and the label slice by slice in specific directories.
                '''
                eval_class.save_slice_img_label(img_volume=volum_flair, pre_volume=label_WT,
                                                gt_volume=stage1_label, output_path=slice_path_WT,
                                                file_name=os.path.basename(file_path) + '({})'.format(
                                                    np.round(k_dice_c_WT[1], decimals=3)), show_gt=show_gt)

                eval_class.save_slice_img_label(img_volume=volume_T1ce, pre_volume=label_TC,
                                                gt_volume=stage2_label, output_path=slice_path_TC,
                                                file_name=os.path.basename(file_path) + '({})'.format(
                                                    np.round(k_dice_c_TC[1], decimals=3)), show_gt=show_gt)

                eval_class.save_slice_img_label(img_volume=volume_T1ce, pre_volume=label_NET,
                                                gt_volume=stage3_label, output_path=slice_path_NET,
                                                file_name=os.path.basename(file_path) + '({})'.format(
                                                    np.round(k_dice_c_ET[1], decimals=3)), show_gt=show_gt)
                '''
                c_map_path1 = os.path.join(
                    pre_vol_path, (os.path.basename(file_path) + '.nii.gz'))
                labeling_vol = nib.Nifti1Image(reduce_small_cc, ref_affine)
                nib.save(labeling_vol, c_map_path1)

        #########################################
        # Calculate the mean dice score
        mean_dice_WT_old = np.mean(all_dice_WT, axis=0)
        mean_dice_WT = self.calculate_mean_eval(all_dice_WT)
        mean_dice_WT = np.around(mean_dice_WT, decimals=3)

        mean_dice_TC_old = np.mean(all_dice_TC, axis=0)
        mean_dice_TC = self.calculate_mean_eval(all_dice_TC)
        mean_dice_TC = np.around(mean_dice_TC, decimals=3)

        mean_dice_ET_old = np.mean(all_dice_ET, axis=0)
        mean_dice_ET = self.calculate_mean_eval(all_dice_ET)
        mean_dice_ET = np.around(mean_dice_ET, decimals=3)

        print("\n average dice [WT, TC, ET]: {}".format(
            [mean_dice_WT[1], mean_dice_TC[1], mean_dice_ET[1]]))
        print("old average dice [WT, TC, ET]: {}\n".format(
            [mean_dice_WT_old[1], mean_dice_TC_old[1], mean_dice_ET_old[1]]))

        # Calculate the mean sensitivity score
        mean_sensitivity_WT_old = np.mean(
            all_sensentivity_WT, axis=0)
        mean_sensitivity_WT = self.calculate_mean_eval(all_sensentivity_WT)
        mean_sensitivity_WT = np.around(mean_sensitivity_WT, decimals=3)

        mean_sensitivity_TC_old = np.mean(all_sensentivity_TC, axis=0)
        mean_sensitivity_TC = self.calculate_mean_eval(all_sensentivity_TC)
        mean_sensitivity_TC = np.around(mean_sensitivity_TC, decimals=3)

        mean_sensitivity_ET_old = np.mean(all_sensentivity_ET, axis=0)
        mean_sensitivity_ET = self.calculate_mean_eval(all_sensentivity_ET)
        mean_sensitivity_ET = np.around(mean_sensitivity_ET, decimals=3)

        print(dataset + "  average sensitivity [WT, TC, ET]: {}".format(
            [mean_sensitivity_WT[1], mean_sensitivity_TC[1], mean_sensitivity_ET[1]]))
        print(dataset + "  old average sensitivity [WT, TC, ET]: {}\n".format(
                [
                    mean_sensitivity_WT_old[1],
                    mean_sensitivity_TC_old[1],
                    mean_sensitivity_ET_old[1]]))
        total_mean_dice_WT = 0
        total_mean_dice_TC = 0
        total_mean_dice_ET = 0

        # Background is excluded
        for i in range(1, self.output_chn):
            total_mean_dice_WT += mean_dice_WT[i]
            total_mean_dice_TC += mean_dice_TC[i]
            total_mean_dice_ET += mean_dice_ET[i]

        average_mean_dice = (
            mean_dice_WT[1] + mean_dice_TC[1] + mean_dice_ET[1]) / 3
        average_mean_sensitivity = (
            mean_sensitivity_WT[1] + mean_sensitivity_TC[1] + mean_sensitivity_ET[1]) / 3

        log_ref = "[WT, TC, ET]:  average dice: {}  mean average dice : {} " \
                  "average sensitivity: {}  mean average sensitivity : {}  \n"
        test_log.write(log_ref.format([mean_dice_WT[1],
                                       mean_dice_TC[1],
                                          mean_dice_ET[1]],
                                      average_mean_dice,
                                      [mean_sensitivity_WT[1],
                                          mean_sensitivity_TC[1],
                                          mean_sensitivity_ET[1]],
                                      average_mean_sensitivity))
        test_log.close()

    # test function for cross validation
    def test4crsv(self):
        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        print("Load checkpoint from:", self.chkpoint_dir, self.step)
        start_time = time.time()
        if self.load_chkpoint(self.chkpoint_dir, self.step):
            print(" [*] Succeed!")
        else:
            print(" [!] Failed!")
            return
        self.test(
            counter=self.step,
            logname="test_result.log",
            dataset="test_set",
            save_pred=False,
            save_log_single=True,
            eval_flag=True)

    # test to save the predicted volume label
    def test_generate_map(self):
        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        print("Load checkpoint from: ", self.chkpoint_dir, self.step)
        start_time = time.time()
        if self.load_chkpoint(self.chkpoint_dir, self.step):
            print(" [*] Succeed!")
        else:
            print(" [!] Failed!")
            return
        self.test(
            counter=111,
            logname="generate_map.log",
            dataset="test_set",
            save_pred=True,
            save_log_single=True,
            eval_flag=False)

    # save checkpoint file
    def save_chkpoint(self, checkpoint_dir, model_name, step):
        model_dir = "%s_%s_%s" % (self.batch_size, self.outputI_size, step)
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(
            self.sess,
            os.path.join(
                checkpoint_dir,
                model_name),
            global_step=step)

    # load checkpoint file
    def load_chkpoint(self, checkpoint_dir, step=-1):
        model_dir = "%s_%s_%s" % (self.batch_size, self.outputI_size, step)
        checkpoint_dir = os.path.join(checkpoint_dir, model_dir)
        print(" [*] Load checkpoint from:", str(checkpoint_dir))
        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(
                self.sess, os.path.join(
                    checkpoint_dir, ckpt_name))
            return True
        else:
            return False

    # load C3D model
    def initialize_finetune(self):
        checkpoint_dir = '../outcome/model/C3D_unet_1chn'
        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver_ft.restore(
                self.sess, os.path.join(
                    checkpoint_dir, ckpt_name))
            print("Fine-tuning succeed！")

    # calculate mean dice or jaccard evaluation
    def calculate_mean_eval(self, val_all):
        non_zeros_num = np.count_nonzero(val_all, axis=0)
        val_sum = np.sum(val_all, axis=0)
        val_mean = val_sum / non_zeros_num
        return val_mean

    # merge multiple labels
    def merge_labels(self, WT, TC, ET):
        '''
        This function transfers the metrics from  WT, TC, ET to the real category (NET, edma, ET)
        :param WT: whole tumor prediction
        :param TC: tumor core prediction
        :param ET: enhancing tumor core predicition
        :return: prediction contains all 4 categories(including background)
        '''
        predictions = np.zeros(WT.shape, dtype='int16')
        # 确定标签2
        predictions[WT == 1] = 2
        predictions[TC == 1] = 1
        predictions[ET == 1] = 4
        return predictions


if __name__ == '__main__':
    WT = np.array([[1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [
                  1, 1, 1, 1, 1], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1]])
    TC = np.array([[0, 1, 1, 1, 0], [0, 1, 1, 1, 0], [
                  0, 1, 1, 1, 0], [0, 1, 1, 1, 0], [0, 1, 1, 1, 0]])
    ET = np.array([[0, 0, 1, 0, 0], [0, 0, 1, 0, 0], [
                  0, 0, 1, 0, 0], [0, 0, 1, 0, 0], [0, 0, 1, 0, 0]])