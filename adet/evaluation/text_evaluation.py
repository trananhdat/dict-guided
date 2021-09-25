import contextlib
import copy
import glob
import io
import itertools
import json
import logging
import os
import re
import shutil
import zipfile
from collections import OrderedDict

import editdistance
import numpy as np
import torch
from adet.evaluation import text_eval_script
from detectron2.data import MetadataCatalog
from detectron2.evaluation.evaluator import DatasetEvaluator
from detectron2.utils import comm
from fvcore.common.file_io import PathManager
from pycocotools.coco import COCO
from shapely.geometry import LinearRing, Polygon


class TextEvaluator(DatasetEvaluator):
    """
    Evaluate text proposals and recognition.
    """

    def __init__(self, dataset_name, cfg, distributed, output_dir=None):
        self._tasks = ("polygon", "recognition")
        self._distributed = distributed
        self._output_dir = output_dir

        self._cpu_device = torch.device("cpu")
        self._logger = logging.getLogger(__name__)

        self._metadata = MetadataCatalog.get(dataset_name)
        if not hasattr(self._metadata, "json_file"):
            raise AttributeError(f"json_file was not found in MetaDataCatalog for '{dataset_name}'.")

        json_file = PathManager.get_local_path(self._metadata.json_file)
        with contextlib.redirect_stdout(io.StringIO()):
            self._coco_api = COCO(json_file)

        # use dataset_name to decide eval_gt_path
        if "totaltext" in dataset_name:
            self._text_eval_gt_path = "datasets/evaluation/gt_totaltext.zip"
            self._word_spotting = True
        elif "ctw1500" in dataset_name:
            self._text_eval_gt_path = "datasets/evaluation/gt_ctw1500.zip"
            self._word_spotting = False
        elif "vintext" in dataset_name:
            self._text_eval_gt_path = "datasets/evaluation/test_label.zip"
            self._word_spotting = True
        self._text_eval_confidence = cfg.MODEL.FCOS.INFERENCE_TH_TEST

    def reset(self):
        self._predictions = []

    def process(self, inputs, outputs):
        for input, output in zip(inputs, outputs):
            prediction = {"image_id": input["image_id"]}

            instances = output["instances"].to(self._cpu_device)
            prediction["instances"] = instances_to_coco_json(instances, input["image_id"])
            self._predictions.append(prediction)

    def to_eval_format(self, file_path, temp_dir="temp_det_results", cf_th=0.5):
        def fis_ascii(s):
            a = (ord(c) < 128 for c in s)
            return all(a)

        def de_ascii(s):
            # a = [c for c in s if ord(c) < 128]
            a = [c for c in s]
            outa = ""
            for i in a:
                outa += i
            return outa

        with open(file_path, "r") as f:
            data = json.load(f)
            with open("temp_all_det_cors.txt", "w") as f2:
                for ix in range(len(data)):
                    if data[ix]["score"] > 0.1:
                        outstr = "{}: ".format(data[ix]["image_id"])
                        xmin = 1000000
                        ymin = 1000000
                        xmax = 0
                        ymax = 0
                        for i in range(len(data[ix]["polys"])):
                            outstr = (
                                outstr
                                + str(int(data[ix]["polys"][i][0]))
                                + ","
                                + str(int(data[ix]["polys"][i][1]))
                                + ","
                            )
                        ass = de_ascii(data[ix]["rec"])
                        if len(ass) >= 0:
                            outstr = outstr + str(round(data[ix]["score"], 3)) + ",####" + ass + "\n"
                            f2.writelines(outstr)
                f2.close()
        dirn = temp_dir
        lsc = [cf_th]
        fres = open("temp_all_det_cors.txt", "r").readlines()
        for isc in lsc:
            if not os.path.isdir(dirn):
                os.mkdir(dirn)

            for line in fres:
                line = line.strip()
                s = line.split(": ")
                filename = "{:07d}.txt".format(int(s[0]))
                outName = os.path.join(dirn, filename)
                with open(outName, "a") as fout:
                    ptr = s[1].strip().split(",####")
                    score = ptr[0].split(",")[-1]
                    if float(score) < isc:
                        continue
                    cors = ",".join(e for e in ptr[0].split(",")[:-1])
                    fout.writelines(cors + ",####" + ptr[1] + "\n")
        os.remove("temp_all_det_cors.txt")

    def sort_detection(self, temp_dir):
        origin_file = temp_dir
        output_file = "final_" + temp_dir

        if not os.path.isdir(output_file):
            os.mkdir(output_file)

        files = glob.glob(origin_file + "*.txt")
        files.sort()

        for i in files:
            out = i.replace(origin_file, output_file)
            fin = open(i, "r").readlines()
            fout = open(out, "w")
            for iline, line in enumerate(fin):
                ptr = line.strip().split(",####")
                rec = ptr[1]
                cors = ptr[0].split(",")
                assert len(cors) % 2 == 0, "cors invalid."
                pts = [(int(cors[j]), int(cors[j + 1])) for j in range(0, len(cors), 2)]
                try:
                    pgt = Polygon(pts)
                except Exception as e:
                    print(e)
                    print("An invalid detection in {} line {} is removed ... ".format(i, iline))
                    continue

                if not pgt.is_valid:
                    print("An invalid detection in {} line {} is removed ... ".format(i, iline))
                    continue

                pRing = LinearRing(pts)
                if pRing.is_ccw:
                    pts.reverse()
                outstr = ""
                for ipt in pts[:-1]:
                    outstr += str(int(ipt[0])) + "," + str(int(ipt[1])) + ","
                outstr += str(int(pts[-1][0])) + "," + str(int(pts[-1][1]))
                outstr = outstr + ",####" + rec
                fout.writelines(outstr + "\n")
            fout.close()
        os.chdir(output_file)

        def zipdir(path, ziph):
            # ziph is zipfile handle
            for root, dirs, files in os.walk(path):
                for file in files:
                    ziph.write(os.path.join(root, file))

        zipf = zipfile.ZipFile("../det.zip", "w", zipfile.ZIP_DEFLATED)
        zipdir("./", zipf)
        zipf.close()
        os.chdir("../")
        # clean temp files
        shutil.rmtree(origin_file)
        shutil.rmtree(output_file)
        return "det.zip"

    def evaluate_with_official_code(self, result_path, gt_path):
        return text_eval_script.text_eval_main(
            det_file=result_path, gt_file=gt_path, is_word_spotting=self._word_spotting
        )

    def evaluate(self):
        if self._distributed:
            comm.synchronize()
            predictions = comm.gather(self._predictions, dst=0)
            predictions = list(itertools.chain(*predictions))

            if not comm.is_main_process():
                return {}
        else:
            predictions = self._predictions

        if len(predictions) == 0:
            self._logger.warning("[COCOEvaluator] Did not receive valid predictions.")
            return {}

        coco_results = list(itertools.chain(*[x["instances"] for x in predictions]))
        PathManager.mkdirs(self._output_dir)

        file_path = os.path.join(self._output_dir, "text_results.json")
        self._logger.info("Saving results to {}".format(file_path))
        with PathManager.open(file_path, "w") as f:
            f.write(json.dumps(coco_results))
            f.flush()

        self._results = OrderedDict()

        # eval text
        temp_dir = "temp_det_results/"
        self.to_eval_format(file_path, temp_dir, self._text_eval_confidence)
        result_path = self.sort_detection(temp_dir)
        text_result = self.evaluate_with_official_code(result_path, self._text_eval_gt_path)
        os.remove(result_path)

        # parse
        template = "(\S+): (\S+): (\S+), (\S+): (\S+), (\S+): (\S+)"
        for task in ("e2e_method", "det_only_method"):
            result = text_result[task]
            groups = re.match(template, result).groups()
            self._results[groups[0]] = {groups[i * 2 + 1]: float(groups[(i + 1) * 2]) for i in range(3)}

        return copy.deepcopy(self._results)


def correct_dict(s, dict_path):
    data = open(dict_path).read().split("\n")
    res = ""
    min_dist = 100
    for word in data:
        if editdistance.eval(word.lower(), s.lower()) < min_dist:
            res = word
            min_dist = editdistance.eval(word.lower(), s.lower())
    # print(res, min_dist, s)
    if min_dist < 2:
        s = res
    return s


def correct_strong(s, img_id):
    data = open("./strong_dict/gt_" + str(img_id) + ".txt").read().split("\n")
    res = ""
    min_dist = 100
    for word in data:
        if editdistance.eval(word.lower(), s.lower()) < min_dist:
            res = word
            min_dist = editdistance.eval(word.lower(), s.lower())
    # print(res, min_dist, s)
    if min_dist < 2:
        s = res
    return res


dictionary = "aàáạảãâầấậẩẫăằắặẳẵAÀÁẠẢÃĂẰẮẶẲẴÂẦẤẬẨẪeèéẹẻẽêềếệểễEÈÉẸẺẼÊỀẾỆỂỄoòóọỏõôồốộổỗơờớợởỡOÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠiìíịỉĩIÌÍỊỈĨuùúụủũưừứựửữƯỪỨỰỬỮUÙÚỤỦŨyỳýỵỷỹYỲÝỴỶỸ"


def make_groups():
    groups = []
    i = 0
    while i < len(dictionary) - 5:
        group = [c for c in dictionary[i : i + 6]]
        i += 6
        groups.append(group)
    return groups


groups = make_groups()

TONES = ["", "ˋ", "ˊ", "﹒", "ˀ", "˜"]
SOURCES = ["ă", "â", "Ă", "Â", "ê", "Ê", "ô", "ơ", "Ô", "Ơ", "ư", "Ư", "Đ", "đ"]
TARGETS = ["aˇ", "aˆ", "Aˇ", "Aˆ", "eˆ", "Eˆ", "oˆ", "o˒", "Oˆ", "O˒", "u˒", "U˒", "D-", "d-"]


def correct_tone_position(word):
    word = word[:-1]
    if len(word) < 2:
        pass
    first_ord_char = ""
    second_order_char = ""
    for char in word:
        for group in groups:
            if char in group:
                second_order_char = first_ord_char
                first_ord_char = group[0]
    if word[-1] == first_ord_char and second_order_char != "":
        pair_chars = ["qu", "Qu", "qU", "QU", "gi", "Gi", "gI", "GI"]
        for pair in pair_chars:
            if pair in word and second_order_char in ["u", "U", "i", "I"]:
                return first_ord_char
        return second_order_char
    return first_ord_char


# def decoder(recognition):
#     for char in TARGETS:
#         recognition = recognition.replace(char, SOURCES[TARGETS.index(char)])
#     if len(recognition) < 1:
#         return recognition
#     if recognition[-1] in TONES:
#         if len(recognition) < 2:
#             return recognition
#         replace_char = correct_tone_position(recognition)
#         tone = recognition[-1]
#         recognition = recognition[:-1]
#         for group in groups:
#             if replace_char in group:
#                 recognition = recognition.replace(replace_char, group[TONES.index(tone)])
#     return recognition
def decoder(recognitions):
    recognitions = recognitions.split(' ')
    list_recognition = ''
    for recognition in recognitions:
        for char in TARGETS:
            recognition = recognition.replace(char, SOURCES[TARGETS.index(char)])
        if len(recognition) < 1:
            list_recognition += recognition + ' '
            continue
        if recognition[-1] in TONES:
            if len(recognition) < 2:
                list_recognition += recognition + ' '
                continue
            replace_char = correct_tone_position(recognition)
            tone = recognition[-1]
            recognition = recognition[:-1]
            for group in groups:
                if replace_char in group:
                    recognition = recognition.replace(replace_char, group[TONES.index(tone)])
        list_recognition += recognition + ' '
    return list_recognition

def instances_to_coco_json(instances, img_id):
    num_instances = len(instances)
    if num_instances == 0:
        return []

    scores = instances.scores.tolist()
    beziers = instances.beziers.numpy()
    recs = instances.recs.numpy()

    results = []
    for bezier, rec, score in zip(beziers, recs, scores):
        # convert beziers to polygons
        poly = bezier_to_polygon(bezier)
        s = decode(rec)
        # s = correct_strong(s, img_id)
        s = decoder(s)
        result = {
            "image_id": img_id,
            "category_id": 1,
            "polys": poly,
            "rec": s,
            "score": score,
        }
        results.append(result)
    return results


def bezier_to_polygon(bezier):
    u = np.linspace(0, 1, 20)
    bezier = bezier.reshape(2, 4, 2).transpose(0, 2, 1).reshape(4, 4)
    points = (
        np.outer((1 - u) ** 3, bezier[:, 0])
        + np.outer(3 * u * ((1 - u) ** 2), bezier[:, 1])
        + np.outer(3 * (u ** 2) * (1 - u), bezier[:, 2])
        + np.outer(u ** 3, bezier[:, 3])
    )

    # convert points to polygon
    points = np.concatenate((points[:, :2], points[:, 2:]), axis=0)
    return points.tolist()


CTLABELS = [
    " ",
    "!",
    '"',
    "#",
    "$",
    "%",
    "&",
    "'",
    "(",
    ")",
    "*",
    "+",
    ",",
    "-",
    ".",
    "/",
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    ":",
    ";",
    "<",
    "=",
    ">",
    "?",
    "@",
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
    "N",
    "O",
    "P",
    "Q",
    "R",
    "S",
    "T",
    "U",
    "V",
    "W",
    "X",
    "Y",
    "Z",
    "[",
    "\\",
    "]",
    "^",
    "_",
    "`",
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "h",
    "i",
    "j",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "q",
    "r",
    "s",
    "t",
    "u",
    "v",
    "w",
    "x",
    "y",
    "z",
    "{",
    "|",
    "}",
    "~",
    "ˋ",
    "ˊ",
    "﹒",
    "ˀ",
    "˜",
    "ˇ",
    "ˆ",
    "˒",
    "‑",
]
# CTLABELS = [' ','!','"','#','$','%','&','\'','(',')','*','+',',','-','.','/','0','1','2','3','4','5','6','7','8','9',':',';','<','=','>','?','@','A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W','X','Y','Z','[','\\',']','^','_','`','a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y','z','{','|','}','~']
# CTLABELS = ['^', '\\', '}', 'ỵ', '>', '<', '{', '~', '`', '°', '$', 'ẽ', 'ỷ', 'ẳ', '_', 'ỡ', ';', '=', 'Ẳ', 'j', '[', ']', 'ẵ', '?', 'ẫ', 'Ẵ', 'ỳ', 'Ỡ', 'ẹ', 'è', 'z', 'ỹ', 'ằ', 'õ', 'ũ', 'Ẽ', 'ỗ', 'ỏ', '@', 'Ằ', 'Ỳ', 'Ẫ', 'ù', 'ử', '#', 'Ẹ', 'Z', 'Õ', 'ĩ', 'Ỏ', 'È', 'Ỷ', 'ý', 'Ũ', '*', 'ò', 'é', 'q', 'ở', 'ổ', 'ủ', 'ẩ', 'ã', 'ẻ', 'J', 'ữ', 'ễ', 'ặ', '+', 'ứ', 'Ỹ', 'ự', 'ụ', 'Ỗ', '%', 'ắ', 'ồ', '"', 'ề', 'ể', 'ỉ', 'ợ', '!', 'Ẻ', 'ừ', 'ọ', '&', 'ì', 'É', 'ậ', 'Ù', 'Ặ', 'x', 'Ỉ', 'ú', 'í', 'ó', 'Ẩ', 'ị', 'ế', 'Ứ', 'â', 'ấ', 'ầ', 'ớ', 'ă', 'Ủ', 'Ĩ', '(', 'Ắ', 'Ừ', ')', 'ờ', 'Ý', 'Ễ', 'Ã', 'ô', 'ộ', 'Ữ', 'Ợ', 'ả', 'Ở', 'ệ', 'W', 'ơ', 'Ổ', 'ố', 'Ề', 'f', 'Ử', 'ạ', 'w', 'Ò', 'Ự', 'Ụ', 'Ú', 'Ồ', 'ê', 'Ó', 'Ì', 'b', 'Í', 'Ể', 'đ', 'Ớ', '/', 'k', 'Ă', 'v', 'Ị', 'Ậ', 'Ọ', 'd', 'Ầ', 'Ấ', 'ư', 'á', 'Ế', ' ', 'p', 'Ơ', 'F', 'Ả', 'Ộ', 'Ê', 'Ờ', 's', '-', 'à', 'y', 'Ố', 'l', 'Â', 'Q', ',', 'X', 'Ệ', 'Ạ', 'Ô', 'r', ':', '6', '7', 'u', '4', 'm', '5', 'e', '8', 'c', 'Ư', 'Á', '9', 'D', '3', 'o', '.', 'Y', 'g', 'K', 'a', 'À', 't', '2', 'B', 'E', 'V', 'R', '1', 'S', 'i', 'L', 'P', 'Đ', 'h', 'U', '0', 'M', 'O', 'n', 'A', 'G', 'I', 'C', 'T', 'H', 'N']


def ctc_decode(rec):
    # ctc decoding
    last_char = False
    s = ""
    for c in rec:
        c = int(c)
        if c < 104:
            if last_char != c:
                s += CTLABELS[c]
                last_char = c
        elif c == 104:
            s += "口"
        else:
            last_char = False
    return s


def decode(rec):
    s = ""
    for c in rec:
        c = int(c)
        if c < 104:
            s += CTLABELS[c]
        elif c == 104:
            s += "口"

    return s
