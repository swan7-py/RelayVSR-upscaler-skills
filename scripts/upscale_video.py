# -*- coding: utf-8 -*-
"""
upscale_video.py —— 本地 ComfyUI + RelayVSR x2 视频放大

设计要点（来自放大测试项目实测）：
  - 长视频一次性整段跑会因显存/内存超限假死，故**分段入队 + 模型常驻 + 硬切拼接**
  - 分段默认 118 帧（24fps ≈ 4.9s），在 RTX 5080 16GB 上是稳定档
  - 全程以 1s 粒度采样显存/内存，用 p90 当工作集、max 仅作上限参考
  - 末段从原片直接取音轨（-c copy），避免重编码

用法：
  python upscale_video.py --src "<源视频>" --dst "<输出视频>" [--seg 118] [--detail 0.8]

前置：本地 ComfyUI 已在 127.0.0.1:8188 运行；RelayVSR 模型与 VAE 已就位。
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WF = os.path.join(HERE, "..", "assets", "relayvsr_x2_video_api.json")

HOST = "http://127.0.0.1:8188"
# ComfyUI 安装目录下的 input / output（按本机实际位置调整）
DEFAULT_INDIR = "E:/ComfyUI/input"
DEFAULT_OUTDIR = "E:/ComfyUI/output/upscale"

UNET_X2 = "relayvsr\\relayvsr_x2_lightx2v_int8_convrot.safetensors"
VAE_X2 = "relayvsr_flash_vae_x2_fp32.safetensors"


def api(path, data=None, timeout=60):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(HOST + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe_frames(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
             "-show_entries", "stream=nb_read_frames,width,height,r_frame_rate",
             "-of", "json", path])
    if not r.stdout.strip():
        return 0, [], None
    streams = json.loads(r.stdout).get("streams") or []
    if not streams:
        return 0, [], None
    s = streams[0]
    return int(s.get("nb_read_frames", 0)), [str(s["width"]), str(s["height"])], s["r_frame_rate"]


def detect_fps(fr):
    """把 '24000/1001' 或 '24/1' 转成 float，无法解析返回 24.0"""
    if not fr:
        return 24.0
    try:
        if "/" in fr:
            a, b = fr.split("/")
            return float(a) / float(b) if float(b) else 24.0
        return float(fr)
    except Exception:
        return 24.0


class Sampler(threading.Thread):
    def __init__(self, path):
        super().__init__(daemon=True)
        self.t0 = time.time()
        self.stop_flag = False
        self.path = path
        self.vram, self.ram = [], []

    def run(self):
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_s", "vram_mib", "ram_mib"])
            f.flush()
            try:
                while not self.stop_flag:
                    try:
                        v = int(run(["nvidia-smi", "--query-gpu=memory.used",
                                     "--format=csv,noheader,nounits"], timeout=5
                                    ).stdout.strip().splitlines()[0])
                    except Exception:
                        v = -1
                    r = int(psutil.virtual_memory().used / 1024 ** 2) if HAVE_PSUTIL else -1
                    if v > 0:
                        self.vram.append(v)
                    if r > 0:
                        self.ram.append(r)
                    w.writerow([round(time.time() - self.t0, 1), v, r])
                    f.flush()
                    time.sleep(1.0)
            finally:
                f.close()

    def stop(self):
        self.stop_flag = True
        self.join(timeout=3)


def make_wf(clip, prefix, detail, wf_path, unet, vae):
    with open(wf_path, encoding="utf-8") as f:
        wf = json.load(f)
    for nid, node in wf.items():
        if node.get("class_type") == "LoadVideo":
            node["inputs"]["file"] = clip
            node["inputs"].pop("video-preview", None)
    if unet:
        wf["4"]["inputs"]["unet_name"] = unet
    if vae:
        wf["7"]["inputs"]["vae_name"] = vae
    wf["104"]["inputs"]["detail"] = detail
    wf["106"]["inputs"]["filename_prefix"] = f"upscale/{prefix}"
    return wf


def p90(vals):
    if not vals:
        return -1
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def main():
    ap = argparse.ArgumentParser(description="RelayVSR x2 视频放大")
    ap.add_argument("--src", required=True, help="源视频路径")
    ap.add_argument("--dst", required=True, help="输出视频路径")
    ap.add_argument("--workflow", default=DEFAULT_WF, help="RelayVSR 工作流 JSON")
    ap.add_argument("--host", default="http://127.0.0.1:8188", help="ComfyUI 地址")
    ap.add_argument("--indir", default=DEFAULT_INDIR, help="ComfyUI input 目录")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help="ComfyUI output/upscale 目录")
    ap.add_argument("--seg", type=int, default=118, help="每段帧数")
    ap.add_argument("--detail", type=float, default=0.8, help="RelayVSR detail 参数")
    ap.add_argument("--fps", type=float, default=0.0, help="源帧率，0=自动探测")
    ap.add_argument("--tag", default="RELAYX2", help="文件/队列标签")
    ap.add_argument("--unet", default=UNET_X2, help="UNET 文件名（含子目录）")
    ap.add_argument("--vae", default=VAE_X2, help="VAE 文件名（含子目录）")
    ap.add_argument("--timeout", type=int, default=7200, help="总超时秒")
    ap.add_argument("--workdir", default=None, help="中间文件目录，默认系统临时目录")
    args = ap.parse_args()

    global HOST
    HOST = args.host.rstrip("/")

    if not os.path.exists(args.workflow):
        raise SystemExit(f"工作流文件不存在：{args.workflow}")
    os.makedirs(args.indir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.dst)), exist_ok=True)
    workdir = args.workdir or tempfile.mkdtemp(prefix=f"{args.tag}_")
    os.makedirs(workdir, exist_ok=True)

    total, wh, fr = probe_frames(args.src)
    if total <= 0:
        raise SystemExit("源视频帧数探测失败")
    fps = args.fps if args.fps > 0 else detect_fps(fr)
    nseg = (total + args.seg - 1) // args.seg
    print(f"=== {os.path.basename(args.src)} {wh} {total} 帧 {fps:.3f}fps "
          f"→ {nseg} 段 × {args.seg} | x2 detail {args.detail} ===", flush=True)

    # ---- 切段（-an：分段无音轨，末段从原片取音轨）----
    paths = []
    for i in range(nseg):
        start, count = i * args.seg, min(args.seg, total - i * args.seg)
        p = os.path.join(args.indir, f"{args.tag}_s{i:03d}.mp4")
        paths.append(p)
        if os.path.exists(p) and probe_frames(p)[0] == count:
            continue
        r = run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start / fps:.6f}", "-i", args.src,
                 "-frames:v", str(count), "-fps_mode", "cfr", "-r", f"{fps:.6f}",
                 "-c:v", "libx264", "-crf", "10", "-preset", "fast",
                 "-pix_fmt", "yuv420p", "-an", p], timeout=900)
        if r.returncode != 0:
            raise SystemExit(f"段 {i} 切分失败：{r.stderr[-500:]}")
        nf = probe_frames(p)[0]
        if nf != count:
            raise SystemExit(f"段 {i} 帧数不符：期望 {count} 实际 {nf}")
    print(f"  {nseg} 段输入就绪（{total} 帧）", flush=True)

    # ---- 队列状态确认 ----
    q = api("/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        raise SystemExit("ComfyUI 队列非空，先等待/清空再跑")
    try:
        api("/free", {"unload_models": True, "free_memory": True})
    except Exception as e:
        print("  /free 忽略：", e)
    time.sleep(3)

    s = Sampler(os.path.join(workdir, f"{args.tag}_run.csv"))
    s.start()

    def submit(i):
        prefix = f"{args.tag}_o{i:03d}"
        try:
            return api("/prompt", {"prompt": make_wf(os.path.basename(paths[i]), prefix,
                                                      args.detail, args.workflow, args.unet, args.vae),
                                   "client_id": args.tag})["prompt_id"]
        except urllib.error.HTTPError as e:
            raise SystemExit(f"段 {i} 提交失败：{e.read().decode('utf-8', 'ignore')}")

    pids = [submit(i) for i in range(nseg)]
    print(f"  {nseg} 段已全部入队，模型常驻", flush=True)

    deadline, done, last = time.time() + args.timeout, {}, 0

    def collect(hist):
        for i, pid in enumerate(pids):
            if i in done or pid not in hist:
                continue
            st = hist[pid].get("status", {})
            if st.get("completed") or st.get("status_str") in ("success", "error"):
                done[i] = st.get("status_str") or ("success" if st.get("completed") else "unknown")
                if done[i] != "success":
                    print(f"  ⚠️ 段 {i} 状态 {done[i]}", flush=True)

    while time.time() < deadline and len(done) < nseg:
        time.sleep(5)
        try:
            q = api("/queue")
            if not q.get("queue_running") and not q.get("queue_pending"):
                collect(api("/history"))
        except Exception as e:
            print("  轮询异常（忽略）：", str(e)[:100], flush=True)
        if len(done) - last >= 3 or len(done) == nseg:
            last = len(done)
            print(f"  完成 {last}/{nseg} 段 ({(time.time()-s.t0)/60:.1f} 分钟)", flush=True)
    try:
        collect(api("/history"))
    except Exception:
        pass

    bad = [i for i in range(nseg) if done.get(i) != "success"]
    if bad:
        s.stop()
        raise SystemExit(f"未完成/失败段：{bad}")
    print(f"  全部成功，用时 {(time.time()-s.t0)/60:.1f} 分钟", flush=True)

    # ---- 收集产物 + 逐段帧数校验 ----
    seg_out, expect_total = [], 0
    for i in range(nseg):
        prefix = f"{args.tag}_o{i:03d}"
        cand = [os.path.join(args.outdir, f) for f in os.listdir(args.outdir)
                if f.startswith(prefix) and f.lower().endswith((".mp4", ".webm", ".mkv"))]
        if not cand:
            s.stop()
            raise SystemExit(f"段 {i} 输出缺失")
        f = max(cand, key=os.path.getmtime)
        nf, wh_out, _ = probe_frames(f)
        want = min(args.seg, total - i * args.seg)
        if nf != want:
            print(f"  ⚠️ 段 {i}: 输出 {nf} 帧，期望 {want}", flush=True)
        expect_total += nf
        if i in (0, nseg - 1, 1):
            print(f"  段 {i:03d}: {nf} 帧 {wh_out}", flush=True)
        seg_out.append(f)

    lst = os.path.join(workdir, f"{args.tag}_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in seg_out:
            f.write("file '" + p.replace("\\", "/") + "'\n")
    tmp = os.path.join(workdir, f"{args.tag}_silent.mp4")
    r = run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", lst, "-map", "0:v:0", "-c:v", "copy", "-an", tmp], timeout=1800)
    if r.returncode != 0:
        s.stop()
        raise SystemExit("concat 失败：" + r.stderr[-800:])
    nf_cat, _, _ = probe_frames(tmp)
    print(f"  拼接 {nf_cat} 帧（期望 {expect_total}）", flush=True)

    # ---- 原音轨（不截断，避免吃掉尾帧）----
    has_audio = run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=index", "-of", "csv=p=0", args.src
                     ]).stdout.strip() != ""
    if has_audio:
        r = run(["ffmpeg", "-y", "-v", "error", "-i", tmp, "-i", args.src,
                 "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                 "-c:a", "aac", "-b:a", "192k", args.dst], timeout=1800)
        if r.returncode != 0:
            s.stop()
            raise SystemExit("合并音轨失败：" + r.stderr[-800:])
        os.remove(tmp)
    else:
        os.replace(tmp, args.dst)
    s.stop()

    nf, wh, _ = probe_frames(args.dst)
    if not args.workdir:
        try:
            os.rmdir(workdir)
        except OSError:
            pass
    summary = {"model": "RelayVSR x2", "detail": args.detail, "seg": args.seg, "nseg": nseg,
               "src": args.src, "in_frames": total, "out_frames": nf, "res": wh,
               "minutes": round((time.time() - s.t0) / 60, 1),
               "sec_per_frame": round((time.time() - s.t0) / max(1, total), 3),
               "vram_p90": p90(s.vram), "vram_max": max(s.vram) if s.vram else -1,
               "ram_max": max(s.ram) if s.ram else -1,
               "size_gb": round(os.path.getsize(args.dst) / 1024 ** 3, 3),
               "audio": "aac_192k_reencode" if has_audio else "none", "dst": args.dst}
    out_json = os.path.join(workdir, f"{args.tag}_result.json")
    json.dump(summary, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("=== 交付 ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"运行时日志：{out_json}", flush=True)


if __name__ == "__main__":
    main()
