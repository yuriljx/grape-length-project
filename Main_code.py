# ========== [Part 1] Colab Environment Setup ==========
from google.colab import drive
import os

# Mount Google Drive
drive.mount('/content/drive')

# Check if successfully mounted
if os.path.exists("/content/drive/MyDrive"):
    print("✅ Google Drive successfully mounted")
else:
    print("❌ Google Drive not mounted, please re-run drive.mount() if needed")

# Install required dependencies
!pip install -q ultralytics
!pip install -q scipy

print("✅ Ultralytics & scipy installation finished")

# ========== [Part 2] Imports ==========
import random
import numpy as np
import torch
import json
import glob
import shutil
import pandas as pd
import cv2
import matplotlib.pyplot as plt
import locale

from ultralytics import YOLO
from datetime import datetime
import math

import matplotlib.dates as mdates
from matplotlib.dates import DateFormatter, DayLocator
from matplotlib.ticker import FixedLocator, Formatter
from scipy.optimize import curve_fit

import matplotlib.lines as mlines
import matplotlib.patches as mpatches

# ========== [Part 3] Global Settings & Example Paths ==========
def set_random_seed(seed=42):
    """
    Set random seed for reproducibility.
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

# Please modify the following paths according to your environment
base_path = "/content/drive/MyDrive/DeepLearning_Projects/Grape_Length/Training_Pics"
time_series_path = "/content/drive/MyDrive/DeepLearning_Projects/Grape_Length/Time_Series_Sample_K5"
coco_json_path = os.path.join(base_path, "labels_my-project-name_2025-03-06-03-02-19.json")
dataset_yaml_path = os.path.join(base_path, "dataset.yaml")

# Whether to enable training
ENABLE_TRAINING = True

# ========== [Part 4] Smoothing Functions ==========
def smooth_diameter(df, window=3, z_threshold=2.5):
    """
    Perform smoothing on 'average_diameter', discard outliers that exceed the threshold,
    then do interpolation and rolling average.
    """
    values = df["average_diameter"].values.astype(float)
    half_w = window // 2
    for i in range(len(values)):
        left = max(0, i - half_w)
        right = min(len(values), i + half_w + 1)
        local_slice = values[left:right]
        local_valid = local_slice[~np.isnan(local_slice)]
        if len(local_valid) < 2:
            continue
        mean_ = np.mean(local_valid)
        std_ = np.std(local_valid)
        if std_ > 0 and not np.isnan(values[i]):
            z_score = abs(values[i] - mean_) / std_
            if z_score > z_threshold:
                values[i] = np.nan
    ser = pd.Series(values)
    ser = ser.interpolate(method='linear', limit_direction='both')
    ser = ser.rolling(window, center=True, min_periods=1).mean()
    return ser.values

def force_smooth_diameter(dates, smoothed_vals,
                          cutoff_str="2024-10-10",
                          max_drop=0.05):
    """
    Force smoothing with specific constraints:
      1) Before 'cutoff_str', ensure non-decreasing behavior
      2) After 'cutoff_str', limit the maximum drop per step
    """
    date_cut = pd.to_datetime(cutoff_str).date()
    forced_vals = smoothed_vals.copy()
    dt_array = pd.to_datetime(dates).dt.date

    # Before cutoff: non-decreasing
    mask_pre = dt_array < date_cut
    forced_pre = forced_vals[mask_pre]
    if len(forced_pre) > 0:
        forced_pre = np.maximum.accumulate(forced_pre)
        forced_vals[mask_pre] = forced_pre

    # After cutoff: limit the drop
    mask_post = dt_array >= date_cut
    post_indices = np.where(mask_post)[0]
    for i in post_indices:
        if i == 0:
            continue
        prev_val = forced_vals[i - 1]
        current_val = forced_vals[i]
        allowed_min = prev_val - max_drop
        if current_val < allowed_min:
            forced_vals[i] = allowed_min

    # Additional smoothing for post-cutoff
    if len(forced_vals[mask_post]) > 0:
        forced_vals[mask_post] = pd.Series(forced_vals[mask_post]) \
            .rolling(3, min_periods=1, center=True).mean()

    return forced_vals

# ========== [Part 5] Convert COCO to YOLO Format ==========
def convert_coco_to_yolo():
    """
    Convert COCO-format JSON annotations to YOLO txt labels.
    """
    labels_path = os.path.join(base_path, "labels")
    shutil.rmtree(labels_path, ignore_errors=True)
    os.makedirs(labels_path, exist_ok=True)

    with open(coco_json_path,"r",encoding="utf-8") as f:
        data = json.load(f)
    image_id_to_data = {img["id"]: img for img in data["images"]}

    for ann in data["annotations"]:
        image_id = ann["image_id"]
        if image_id not in image_id_to_data:
            continue
        image_name = image_id_to_data[image_id]["file_name"]
        base_name = os.path.splitext(os.path.basename(image_name))[0]
        lbl_file = os.path.join(labels_path, base_name + ".txt")

        bbox = ann["bbox"]
        img_w = image_id_to_data[image_id]["width"]
        img_h = image_id_to_data[image_id]["height"]
        x_center = (bbox[0] + bbox[2]/2)/img_w
        y_center = (bbox[1] + bbox[3]/2)/img_h
        w = bbox[2]/img_w
        h = bbox[3]/img_h

        with open(lbl_file,"a") as lf:
            lf.write(f"0 {x_center} {y_center} {w} {h}\n")

    print("✅ COCO → YOLO label conversion complete")

# ========== [Part 6] Create dataset.yaml ==========
def create_dataset_yaml():
    """
    Randomly split images folder into train/val sets and generate dataset.yaml
    """
    images_path = os.path.join(base_path, "images")
    all_images = sorted(glob.glob(os.path.join(images_path,"*.[jJ][pP][gG]")))
    if not all_images:
        raise ValueError(f"No JPG files found in {images_path}")

    # Random split
    np.random.seed(42)
    np.random.shuffle(all_images)
    split_idx = int(len(all_images)*0.8)
    train_imgs = all_images[:split_idx]
    val_imgs = all_images[split_idx:]

    train_img_dir = os.path.join(base_path,"train","images")
    train_lbl_dir = os.path.join(base_path,"train","labels")
    val_img_dir   = os.path.join(base_path,"val","images")
    val_lbl_dir   = os.path.join(base_path,"val","labels")
    for d in [train_img_dir,train_lbl_dir,val_img_dir,val_lbl_dir]:
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)

    labels_root = os.path.join(base_path, "labels")
    for img_path in train_imgs:
        shutil.copy(img_path, train_img_dir)
        base_name = os.path.splitext(os.path.basename(img_path))[0]
        label_txt = os.path.join(labels_root, base_name + ".txt")
        if os.path.exists(label_txt):
            shutil.copy(label_txt, train_lbl_dir)

    for img_path in val_imgs:
        shutil.copy(img_path, val_img_dir)
        base_name = os.path.splitext(os.path.basename(img_path))[0]
        label_txt = os.path.join(labels_root, base_name+".txt")
        if os.path.exists(label_txt):
            shutil.copy(label_txt, val_lbl_dir)

    base_path_fixed = base_path.replace("\\","/")
    yaml_content = f"""path: {base_path_fixed}
train: train/images
val: val/images
nc: 1
names: ['Grape']
"""
    with open(dataset_yaml_path,"w",encoding="utf-8") as f:
        f.write(yaml_content)

    print("✅ dataset.yaml generated")

# ========== [Part 7] Load Scale Points & Compute Scale ==========
def load_scale_points(scale_csv):
    """
    Load scale point data to calculate pixel distance per 1 cm in images.
    """
    try:
        locale.setlocale(locale.LC_ALL,'en_US.UTF-8')
    except:
        pass
    df = pd.read_csv(scale_csv, header=None, encoding='latin1')
    df.columns = ["point","x","y","image","img_width","img_height"]
    return df

def compute_scale(image_name, scale_data):
    """
    Find scale_0cm and scale_1cm in the CSV for the specified image, and compute distance.
    """
    if scale_data.empty:
        return None
    image_name_lower = image_name.lower()
    scale_data["image"] = scale_data["image"].str.lower()
    pts = scale_data[scale_data["image"]==image_name_lower]
    pt1 = pts[pts["point"]=="scale_0cm"]
    pt2 = pts[pts["point"]=="scale_1cm"]
    if pt1.empty or pt2.empty:
        return None
    c1 = pt1.iloc[0][["x","y"]].values.astype(float)
    c2 = pt2.iloc[0][["x","y"]].values.astype(float)
    dist = np.linalg.norm(c1 - c2)
    return dist, (int(c1[0]), int(c1[1])), (int(c2[0]), int(c2[1]))

# ========== [Part 8] Training Function ==========
def run_training():
    """
    Train YOLOv8s and save relevant metrics.
    """
    # Fix random seed
    set_random_seed(42)

    # 1) Training parameters
    training_params = {
        "model": "yolov8s.pt",
        "epochs": 120,
        "imgsz": 640,
        "batch": 16,
        "device": "cuda",

        # Hyperparameters
        "lr0": 0.02,
        "lrf": 0.01,
        "momentum": 0.937,
        "weight_decay": 0.001,
        "warmup_epochs": 3.0,
        "warmup_momentum": 0.8,
        "box": 7.5,
        "cls": 0.5,
        "hsv_h": 0.015,
        "hsv_s": 0.7,
        "hsv_v": 0.4,
        "degrees": 5.0,
        "translate": 0.1,
        "scale": 0.5,
        "shear": 0.0,
        "perspective": 0.0,
        "flipud": 0.0,
        "fliplr": 0.5,
        "mosaic": 0.8,
        "mixup": 0.0,
        "copy_paste": 0.0,

        # Other settings
        "augment": True,
        "patience": 50,

        # Inference/Validation settings
        "inference_conf_threshold": 0.25,
        "inference_iou_threshold": 0.5,
    }

    # 2) Training
    model = YOLO(training_params["model"])
    model.train(
        data=dataset_yaml_path,
        epochs=training_params["epochs"],
        imgsz=training_params["imgsz"],
        batch=training_params["batch"],
        device=training_params["device"],
        project=os.path.join(base_path, "runs", "detect"),
        name="trainX",
        exist_ok=True,

        optimizer="SGD",

        lr0=training_params["lr0"],
        lrf=training_params["lrf"],
        momentum=training_params["momentum"],
        weight_decay=training_params["weight_decay"],
        warmup_epochs=training_params["warmup_epochs"],
        warmup_momentum=training_params["warmup_momentum"],
        box=training_params["box"],
        cls=training_params["cls"],
        hsv_h=training_params["hsv_h"],
        hsv_s=training_params["hsv_s"],
        hsv_v=training_params["hsv_v"],
        degrees=training_params["degrees"],
        translate=training_params["translate"],
        scale=training_params["scale"],
        shear=training_params["shear"],
        perspective=training_params["perspective"],
        flipud=training_params["flipud"],
        fliplr=training_params["fliplr"],
        mosaic=training_params["mosaic"],
        mixup=training_params["mixup"],
        copy_paste=training_params["copy_paste"],

        augment=training_params["augment"],
        patience=training_params["patience"],
    )

    # 3) Validation after training
    metrics = model.val(
        conf=training_params["inference_conf_threshold"],
        iou=training_params["inference_iou_threshold"]
    )

    # 4) Extract PR-curve data
    print("Available curves:", metrics.curves)
    target_name = None
    for name in metrics.curves:
        if "pr" in name.lower():
            target_name = name
            break
    if target_name is None:
        print("❌ No 'PR' curve data found.")
    else:
        idx = metrics.curves.index(target_name)
        pr_data = metrics.curves_results[idx]
        recall_values = pr_data[0]
        precision_values = np.squeeze(pr_data[1])
        if len(recall_values) == len(precision_values):
            pr_df = pd.DataFrame({
                'recall': recall_values,
                'precision': precision_values
            })
            pr_curve_csv = os.path.join(base_path, "runs", "detect", "trainX", "PR_curve_data.csv")
            pr_df.to_csv(pr_curve_csv, index=False)
            print(f"✅ PR curve data saved to: {pr_curve_csv}")
        else:
            print("❌ Mismatch in PR data length, unable to generate DataFrame.")

    # 5) Save training metrics
    metrics_path = os.path.join(base_path, "runs", "detect", "trainX", "metrics.txt")
    with open(metrics_path, "w") as f:
        f.write("=== Training & Inference Params ===\n")
        for k, v in training_params.items():
            f.write(f"{k}: {v}\n")

        f.write("\n=== Evaluation Metrics ===\n")
        f.write(f"mAP50: {metrics.box.map50}\n")
        f.write(f"mAP50-95: {metrics.box.map}\n")
        f.write(f"Precision: {metrics.box.p[0]}\n")
        f.write(f"Recall: {metrics.box.r[0]}\n")
        f.write(f"F1: {metrics.box.f1[0]}\n")

    print(f"✅ Training metrics saved to: {metrics_path}")

# ========== [Part 9] Utilities: Get Latest Model & Quick Visualization ==========
def get_latest_model():
    """
    Return the latest best.pt checkpoint from training.
    """
    weight_paths = glob.glob(os.path.join(
        base_path,"runs","detect","train*","weights","best.pt"
    ))
    if not weight_paths:
        raise FileNotFoundError("No best.pt found, please train first.")
    return sorted(weight_paths)[-1]

def predict_and_visualize():
    """
    Simple inference check only; does not save any visualization results.
    """
    print("=> predict_and_visualize() performs a quick detection check (no files saved).")
    model_path = get_latest_model()
    model = YOLO(model_path)

    scale_csv_path = os.path.join(time_series_path, "Marklabel.csv")
    scale_data = load_scale_points(scale_csv_path)

    target_images = sorted(glob.glob(os.path.join(time_series_path, "*.[jJ][pP][gG]")),
                           key=os.path.getctime)
    if not target_images:
        print(f"❌ No JPG images found in {time_series_path}, cannot run detection!")
        return

    for img_path in target_images:
        _ = model.predict(
            img_path,
            conf=0.25,
            iou=0.5,
            save=False,
            show=False
        )
    print("✅ Simple detection completed (no visualization saved).")

# ========== [Part 10] Double-S Function & Time-Series Detection / Statistics / Plotting ==========
def double_sigmoid(x, A1, B1, C1, A2, B2, C2, D):
    """
    Double-S function for fitting grape growth curves.
    """
    return (A1 / (1 + np.exp(-C1*(x - B1))) +
            A2 / (1 + np.exp(-C2*(x - B2))) +
            D)

class MinorTickFormatter(Formatter):
    """
    Custom Formatter for minor ticks on the top axis.
    """
    def __init__(self, label_map):
        super().__init__()
        self.label_map = label_map
    def __call__(self, x, pos=None):
        return self.label_map.get(x, "")

def auto_offset_labels(positions, labels, min_gap=1.0, offset_left=-3.0, offset_right=3.0):
    """
    If adjacent labels are too close, apply left/right offsets.
    """
    new_positions = positions.copy()
    i = 0
    while i < (len(positions)-1):
        dist = positions[i+1] - positions[i]
        if dist < min_gap:
            new_positions[i]   = positions[i]   + offset_left
            new_positions[i+1] = positions[i+1] + offset_right
            i += 2
        else:
            i += 1
    return new_positions

def predict_time_series(
    sample_folder=time_series_path,
    marklabel_csv=None,
    model_path=None,
    output_dir=None,

    # Optional adjustable parameters
    show_lineplot=True,
    show_boxplot=True,
    boxplot_day_interval=15,
    show_box_date_label=True,
    show_original=True,
    show_smoothed=True,
    show_f_smoothed=True,
    show_sd=False,
    show_se=False,
    show_trend=True,
    show_peak_date=True,
    show_phase_lines=False,
    offset_factor=2.0,
    show_95ci=False
):
    """
    Perform object detection on time-series images, measure diameters by date,
    and output various plots (smoothed curves, box plots) plus data files.
    """
    if marklabel_csv is None:
        marklabel_csv = os.path.join(sample_folder,"Marklabel.csv")
    if not model_path:
        model_path = get_latest_model()

    model = YOLO(model_path)
    inference_params = {
        "conf_threshold": 0.25,
        "iou_threshold": 0.5,
        "model_path": model_path,
    }

    images = sorted(glob.glob(os.path.join(sample_folder,"*.[jJ][pP][gG]")))
    if not images:
        print(f"❌ No JPG images found in {sample_folder}!")
        return

    scale_data = load_scale_points(marklabel_csv)
    if output_dir is None:
        output_dir = os.path.join(sample_folder,"results_time_series")
    os.makedirs(output_dir, exist_ok=True)

    # ============= 1) Inference & Recording =============
    records = []
    for img_path in images:
        base_name = os.path.basename(img_path)
        date_str = base_name[:8]
        try:
            date_obj = datetime.strptime(date_str, "%Y%m%d").date()
        except:
            date_obj = None

        sc = compute_scale(base_name, scale_data)
        scale_len = sc[0] if sc else None

        results = model.predict(img_path,
                                conf=inference_params["conf_threshold"],
                                iou=inference_params["iou_threshold"],
                                save=False, show=False)
        diams = []
        for res in results:
            for box in res.boxes.xyxy:
                x1,y1,x2,y2 = map(int, box[:4])
                if scale_len and scale_len>0:
                    d = round((x2 - x1)/scale_len,2)
                    diams.append(d)

        if diams:
            avg_d = round(np.mean(diams),2)
            std_dev = round(np.std(diams, ddof=1),3)
            std_err = round(std_dev / math.sqrt(len(diams)),3)
        else:
            avg_d, std_dev, std_err = None, None, None

        records.append({
            "filename": base_name,
            "date": str(date_obj) if date_obj else "",
            "diameters": diams,
            "average_diameter": avg_d,
            "std_dev": std_dev,
            "std_err": std_err
        })

    df = pd.DataFrame(records)
    df_filtered = df[df["date"] != ""].copy()
    df_filtered.dropna(subset=["average_diameter"], inplace=True)
    df_filtered["date"] = pd.to_datetime(df_filtered["date"])
    if df_filtered.empty:
        print("⚠️ No valid date or average diameter data to plot.")
        return
    df_filtered.sort_values("date", inplace=True)

    # ============= 2) Smoothing =============
    df_filtered["smoothed_diameter"] = smooth_diameter(df_filtered)
    df_filtered["f_smoothed_diameter"] = force_smooth_diameter(
        df_filtered["date"], df_filtered["smoothed_diameter"]
    )
    date2smooth = dict(zip(df_filtered["date"], df_filtered["smoothed_diameter"]))
    date2f = dict(zip(df_filtered["date"], df_filtered["f_smoothed_diameter"]))
    df["smoothed_diameter"] = df["date"].apply(lambda d: date2smooth.get(pd.to_datetime(d, errors='coerce'), None))
    df["f_smoothed_diameter"] = df["date"].apply(lambda d: date2f.get(pd.to_datetime(d, errors='coerce'), None))

    # ============= 3) Line Plot =============
    if show_original or show_smoothed or show_f_smoothed or show_trend:
        plt.figure(figsize=(8,5))
        ax = plt.gca()

        handles_list = []
        labels_list = []
        x_vals = df_filtered["date"]
        z_val = 1.96  # 95% CI factor

        # ---- Original
        line_original = patch_original = None
        if show_original:
            y_orig = df_filtered["average_diameter"]
            if show_sd and "std_dev" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_orig, yerr=df_filtered["std_dev"],
                                  fmt='o-', linewidth=1, markersize=3,
                                  color='C0', label=None)
                line_original = err
                label_original = "Original (±SD)"
            elif show_se and "std_err" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_orig, yerr=df_filtered["std_err"],
                                  fmt='o-', linewidth=1, markersize=3,
                                  color='C0', label=None)
                line_original = err
                label_original = "Original (±SE)"
            else:
                line_original, = ax.plot(x_vals, y_orig, 'o-',
                                         linewidth=1, markersize=3,
                                         color='C0', label=None)
                label_original = "Original"

            if show_95ci and show_se and "std_err" in df_filtered.columns:
                y_low = y_orig - z_val * df_filtered["std_err"]
                y_high = y_orig + z_val * df_filtered["std_err"]
                patch_original = ax.fill_between(x_vals, y_low, y_high,
                                                 color='C0', alpha=0.2,
                                                 label=None)

        # ---- Smoothed
        line_smoothed = patch_smoothed = None
        if show_smoothed:
            y_smooth = df_filtered["smoothed_diameter"]
            if show_sd and "std_dev" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_smooth, yerr=df_filtered["std_dev"],
                                  fmt='o-', linewidth=1, markersize=3,
                                  color='C1', label=None)
                line_smoothed = err
                label_smoothed = "Smoothed (±SD)"
            elif show_se and "std_err" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_smooth, yerr=df_filtered["std_err"],
                                  fmt='o-', linewidth=1, markersize=3,
                                  color='C1', label=None)
                line_smoothed = err
                label_smoothed = "Smoothed (±SE)"
            else:
                line_smoothed, = ax.plot(x_vals, y_smooth, 'o-',
                                         linewidth=1, markersize=3,
                                         color='C1', label=None)
                label_smoothed = "Smoothed"

            if show_95ci and show_se and "std_err" in df_filtered.columns:
                y_low = y_smooth - z_val * df_filtered["std_err"]
                y_high = y_smooth + z_val * df_filtered["std_err"]
                patch_smoothed = ax.fill_between(x_vals, y_low, y_high,
                                                 color='C1', alpha=0.2,
                                                 label=None)

        # ---- F-Smoothed
        line_f_smooth = patch_f_smooth = None
        if show_f_smoothed:
            y_f_smooth = df_filtered["f_smoothed_diameter"]
            if show_sd and "std_dev" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_f_smooth, yerr=df_filtered["std_dev"],
                                  fmt='o-', linewidth=1.2, markersize=4,
                                  color='C2', label=None)
                line_f_smooth = err
                label_f_smooth = "F-Smoothed (±SD)"
            elif show_se and "std_err" in df_filtered.columns:
                err = ax.errorbar(x_vals, y_f_smooth, yerr=df_filtered["std_err"],
                                  fmt='o-', linewidth=1.2, markersize=4,
                                  color='C2', label=None)
                line_f_smooth = err
                label_f_smooth = "F-Smoothed (±SE)"
            else:
                line_f_smooth, = ax.plot(x_vals, y_f_smooth, 'o-',
                                         linewidth=1.2, markersize=4,
                                         color='C2', label=None)
                label_f_smooth = "F-Smoothed"

            if show_95ci and show_se and "std_err" in df_filtered.columns:
                y_low = y_f_smooth - z_val * df_filtered["std_err"]
                y_high = y_f_smooth + z_val * df_filtered["std_err"]
                patch_f_smooth = ax.fill_between(x_vals, y_low, y_high,
                                                 color='C2', alpha=0.2,
                                                 label=None)

        # ---- Double-S Trend
        line_ds_trend = None
        fitted_params = None
        if show_trend:
            x_num = (df_filtered["date"] - df_filtered["date"].min()).dt.days.astype(float)
            y_data = df_filtered["f_smoothed_diameter"].values.astype(float)
            if len(x_num) >= 6 and (np.nanmax(y_data) - np.nanmin(y_data) > 0.1):
                x_min, x_max = x_num.min(), x_num.max()
                y_min, y_max = np.nanmin(y_data), np.nanmax(y_data)
                p0 = [
                    (y_max - y_min) * 0.5,
                    x_min + (x_max - x_min) * 0.2,
                    0.1,
                    (y_max - y_min) * 0.5,
                    x_min + (x_max - x_min) * 0.7,
                    0.1,
                    y_min
                ]
                try:
                    popt, _ = curve_fit(double_sigmoid, x_num, y_data, p0=p0, maxfev=20000)
                    fitted_params = popt
                    x_trend = np.linspace(x_min, x_max, 200)
                    y_trend = double_sigmoid(x_trend, *popt)
                    d_start = df_filtered["date"].min()
                    dates_trend = [d_start + pd.Timedelta(days=float(xx)) for xx in x_trend]
                    line_ds_trend, = ax.plot(dates_trend, y_trend,
                                             linestyle='--', linewidth=2,
                                             color='orange', label=None)
                except RuntimeError:
                    print("⚠️ Double-S fitting failed")

        # ---- Peak Date & Phase Lines
        peak_line_handle = None
        phase_line_handle = None

        # Gray vertical lines (phase boundaries)
        if show_phase_lines and fitted_params is not None:
            A1,B1,C1,A2,B2,C2,D = fitted_params
            d_start = df_filtered["date"].min()

            def add_phase_line(xval):
                line_date = d_start + pd.Timedelta(days=float(xval))
                if (line_date >= df_filtered["date"].min()) and (line_date <= df_filtered["date"].max()):
                    ax.axvline(line_date, color='gray', linestyle=':', linewidth=1.4, alpha=0.8)

            if abs(C1)>1e-5:
                add_phase_line(B1 - offset_factor/C1)
                add_phase_line(B1 + offset_factor/C1)
            if abs(C2)>1e-5:
                add_phase_line(B2 - offset_factor/C2)
                add_phase_line(B2 + offset_factor/C2)

            phase_line_handle = mlines.Line2D([], [], color='gray', linestyle=':', label='Phase Lines')

        # Peak date
        if show_peak_date:
            y_f_smooth = df_filtered["f_smoothed_diameter"].values
            if len(y_f_smooth) > 0 and not np.all(np.isnan(y_f_smooth)):
                idx_pk = np.nanargmax(y_f_smooth)
                pk_date = df_filtered["date"].iloc[idx_pk]
                ax.axvline(pk_date, color='red', linestyle='--', linewidth=1.2, alpha=0.8)
                peak_line_handle = mlines.Line2D([], [], color='red', linestyle='--', label='Peak Date')

        # Top axis for phase lines / peak date annotation
        ax_top = ax.twiny()
        ax_top.set_xlim(ax.get_xlim())
        ax_top.set_xticks([])

        line_positions = []
        line_date2label = {}
        line_label_colors = {}

        if show_phase_lines and fitted_params is not None:
            A1,B1,C1,A2,B2,C2,D = fitted_params
            d_start = df_filtered["date"].min()

            def append_phase_line_label(xval):
                line_date = d_start + pd.Timedelta(days=float(xval))
                if (line_date >= df_filtered["date"].min()) and (line_date <= df_filtered["date"].max()):
                    line_num = mdates.date2num(line_date)
                    line_positions.append(line_num)
                    line_date2label[line_num] = line_date.strftime('%m-%d')

            if abs(C1) > 1e-5:
                append_phase_line_label(B1 - offset_factor/C1)
                append_phase_line_label(B1 + offset_factor/C1)
            if abs(C2) > 1e-5:
                append_phase_line_label(B2 - offset_factor/C2)
                append_phase_line_label(B2 + offset_factor/C2)

        if show_peak_date and len(df_filtered["f_smoothed_diameter"]) > 0:
            idx_pk = np.nanargmax(df_filtered["f_smoothed_diameter"].values)
            pk_date = df_filtered["date"].iloc[idx_pk]
            pk_num = mdates.date2num(pk_date)
            line_positions.append(pk_num)
            line_date2label[pk_num] = pk_date.strftime('%m-%d')
            line_label_colors[pk_num] = 'red'

        if line_positions:
            unique_positions = sorted(set(line_positions))
            labels_temp = [line_date2label[pos] for pos in unique_positions]
            new_positions = auto_offset_labels(
                positions=unique_positions,
                labels=labels_temp,
                min_gap=1.0,
                offset_left=-0.3,
                offset_right=0.3
            )
            label_map = {}
            for old, new, lab in zip(unique_positions, new_positions, labels_temp):
                label_map[new] = lab

            ax_top.xaxis.set_minor_locator(FixedLocator(new_positions))
            ax_top.xaxis.set_minor_formatter(MinorTickFormatter(label_map))
            ax_top.tick_params(axis='x', which='minor', labelsize=8, rotation=30)

            for tick in ax_top.get_xticklabels(minor=True):
                pos = tick.get_position()[0]
                tick.set_color(line_label_colors.get(pos, 'gray'))

        ax.set_title("Average Grape Diameter Over Time")
        ax.set_xlabel("Date")
        ax.set_ylabel("Average Diameter (cm)")
        plt.gcf().autofmt_xdate(bottom=0.15, rotation=30, ha='right')

        # Assemble the legend
        if line_original is not None:
            handles_list.append(line_original)
            labels_list.append(label_original)
            if patch_original is not None:
                ci_patch = mpatches.Patch(color='C0', alpha=0.2, label='95% CI (Original)')
                handles_list.append(ci_patch)
                labels_list.append('95% CI (Original)')

        if line_smoothed is not None:
            handles_list.append(line_smoothed)
            labels_list.append(label_smoothed)
            if patch_smoothed is not None:
                ci_patch = mpatches.Patch(color='C1', alpha=0.2, label='95% CI (Smoothed)')
                handles_list.append(ci_patch)
                labels_list.append('95% CI (Smoothed)')

        if line_f_smooth is not None:
            handles_list.append(line_f_smooth)
            labels_list.append(label_f_smooth)
            if patch_f_smooth is not None:
                ci_patch = mpatches.Patch(color='C2', alpha=0.2, label='95% CI (F-Smoothed)')
                handles_list.append(ci_patch)
                labels_list.append('95% CI (F-Smoothed)')

        if line_ds_trend is not None:
            handles_list.append(line_ds_trend)
            labels_list.append("Double-S Trend")

        if peak_line_handle is not None:
            handles_list.append(peak_line_handle)
            labels_list.append("Peak Date")

        if phase_line_handle is not None:
            handles_list.append(phase_line_handle)
            labels_list.append("Phase Lines")

        plt.legend(handles=handles_list, labels=labels_list,
                   loc='upper left', bbox_to_anchor=(1.02,1.0), borderaxespad=0)

        trend_png_path = os.path.join(output_dir, "time_series_trend.png")
        plt.savefig(trend_png_path, dpi=150, bbox_inches='tight')
        plt.show() if show_lineplot else plt.close()
        print(f"✅ Saved {trend_png_path}")

    # ============= 4) Box Plot =============
    grouped = df_filtered.groupby("date")["diameters"].apply(lambda x: sum(x,[]))
    diameters_by_date = dict(grouped)
    dates_list = sorted(diameters_by_date.keys())
    box_data = [diameters_by_date[d] for d in dates_list if len(diameters_by_date[d])>0]
    if box_data:
        positions = [mdates.date2num(d) for d in dates_list]
        earliest_date = min(dates_list)
        latest_date  = max(dates_list)
        start_date = earliest_date - pd.Timedelta(days=2)
        end_date   = latest_date   + pd.Timedelta(days=2)

        fig, ax = plt.subplots(figsize=(9,5))
        bp = ax.boxplot(
            box_data,
            positions=positions,
            widths=2.0,
            patch_artist=True,
            showfliers=True
        )
        for patch in bp['boxes']:
            patch.set_facecolor("lightblue")

        ax.xaxis_date()
        ax.xaxis.set_major_locator(DayLocator(interval=boxplot_day_interval))
        ax.xaxis.set_major_formatter(DateFormatter('%Y-%m-%d'))
        ax.set_xlim([start_date, end_date])
        ax.set_title("Box-Plot of Grape Diameter by Date")
        ax.set_xlabel(f"Date (Major Tick: ~every {boxplot_day_interval} days)")
        ax.set_ylabel("Diameter (cm)")
        fig.autofmt_xdate(bottom=0.15, rotation=30, ha='right')

        if show_box_date_label:
            ax_top2 = ax.twiny()
            ax_top2.set_xlim(ax.get_xlim())
            ax_top2.set_xticks(positions)
            ax_top2.set_xticklabels([d.strftime('%m-%d') for d in dates_list],
                                    rotation=30, ha='left')
            ax_top2.set_xlabel("Box date (MM-DD)")

        boxplot_png_path = os.path.join(output_dir,"time_series_boxplot.png")
        plt.savefig(boxplot_png_path, dpi=150, bbox_inches='tight')
        plt.show() if show_boxplot else plt.close()
        print(f"✅ Saved {boxplot_png_path}")

        # Five-number summary
        summary_records = []
        for d in dates_list:
            data_d = diameters_by_date[d]
            if not data_d:
                continue
            arr = np.array(data_d)
            q1 = np.percentile(arr,25)
            median = np.percentile(arr,50)
            q3 = np.percentile(arr,75)
            iqr = q3 - q1
            lower_bound = q1 - 1.5*iqr
            upper_bound = q3 + 1.5*iqr
            outliers = arr[(arr<lower_bound)|(arr>upper_bound)]
            summary_records.append({
                "date": d.strftime("%Y-%m-%d"),
                "count": len(arr),
                "min": round(arr.min(),3),
                "q1": round(q1,3),
                "median": round(median,3),
                "q3": round(q3,3),
                "max": round(arr.max(),3),
                "IQR": round(iqr,3),
                "lower_bound": round(lower_bound,3),
                "upper_bound": round(upper_bound,3),
                "outlier_count": len(outliers)
            })
        df_box = pd.DataFrame(summary_records)
        box_csv_path = os.path.join(output_dir,"time_series_boxplot_data.csv")
        df_box.to_csv(box_csv_path, index=False)
        print(f"✅ Box-plot data saved to {box_csv_path}")
    else:
        print("⚠️ No boxplot data available")

    # ============= 5) Save CSV and Write Inference Info =============
    csv_path = os.path.join(output_dir,"time_series_measurements.csv")
    df.to_csv(csv_path, index=False)
    print(f"✅ Measurement results saved to: {csv_path}")

    metrics_path = os.path.join(base_path,"runs","detect","trainX","metrics.txt")
    with open(metrics_path,"a",encoding="utf-8") as f:
        f.write("\n=== Inference Params & Stats ===\n")
        for k,v in inference_params.items():
            f.write(f"{k}: {v}\n")
        f.write(f"total_images: {len(images)}\n")
    print(f"✅ Inference info appended to {metrics_path}")

# ========== [Part 11] Main Execution Entry ==========
if __name__=="__main__":
    # Fix random seed to ensure reproducibility in splitting/training
    set_random_seed(42)

    # 1) Convert labels & generate dataset.yaml
    convert_coco_to_yolo()
    create_dataset_yaml()

    # 2) Decide whether to train
    if ENABLE_TRAINING:
        run_training()  # Make sure this is uncommented if you want to train

    # 3) Quick detection visualization (not saved)
    predict_and_visualize()

    # 4) Time-series detection and visualization
    predict_time_series(
        sample_folder=time_series_path,
        marklabel_csv=None,
        model_path=None,
        output_dir=None,

        show_lineplot=True,
        show_boxplot=True,
        boxplot_day_interval=15,
        show_box_date_label=True,

        # Control whether to show Original / Smoothed / F-Smoothed
        show_original=False,
        show_smoothed=False,
        show_f_smoothed=True,

        # Error bars (±SD / ±SE)
        show_sd=False,
        show_se=True,

        # Double-S curve & peak
        show_trend=True,
        show_peak_date=True,

        # Gray vertical lines
        show_phase_lines=True,
        offset_factor=2.0,

        # Whether to display 95% confidence interval (only applies if show_se=True)
        show_95ci=True
    )

    print("✅ All processes completed!")
