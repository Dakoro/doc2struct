# ======= Imports =======
import os
import re
import json
import fire
import textwrap
from typing import List, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from docx import Document
from pypdf import PdfReader

import faiss
from sentence_transformers import SentenceTransformer

# ======= Config =======
MODEL_NAME       = "sentence-transformers/all-MiniLM-L6-v2"  # small, fast
TARGET_CH_LEN    = 350                                      # target chunk size in characters
OVERLAP_CH       = 60                                       # overlap to avoid boundary cuts
NUM_PIVOTS       = 12                                       # max number of pivots (auto‑capped by data size)
KNN              = 16                                       # neighbors for redundancy/coverage
BAND_QUANTILES   = [0.25, 0.5, 0.75]                        # define 4 bands per pivot by distance
SEED             = 17

np.random.seed(SEED)

# ======= Helpers =======
def read_docx_to_paragraphs(fp: str) -> List[str]:
    doc = Document(fp)
    paras = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return paras

def read_txt_to_paragraphs(fp: str) -> List[str]:
    with open(fp, mode='r', encoding='utf-8') as txtfile:
        paras = txtfile.read().split('\n\n')
    return [para.strip() for para in paras if para and para.strip()]

def read_pdf_to_paragraphs(fp: str) -> List[str]:
    reader = PdfReader(fp)
    text = ''
    for n in range(len(reader.pages)):
        page = reader.pages[n]
        text += page.extract_text()
    paras = text.split('\n\n')
    return [para.strip() for para in paras if para and para.strip()]

def read_file(fp):
    _, ext = os.path.splitext(os.path.basename(fp))
    print(f"File Extension: {ext}")
    if ext == '.docx':
        return read_docx_to_paragraphs(fp)
    elif ext == '.pdf':
        return read_pdf_to_paragraphs(fp)
    elif ext == '.txt':
        return read_txt_to_paragraphs(fp)
    return None
        
def split_into_sentences(text: str) -> List[str]:
    # Lightweight sentence splitter (regex). Good enough for prototype.
    sents = re.split(r'(?<=[\.!\?])\s+(?=[A-Z0-9(\"\'])', text.strip())
    # Fallback if text contains no terminal punctuation
    if len(sents) == 1:
        sents = re.split(r'\n+', text.strip())
    return [s.strip() for s in sents if s.strip()]

def make_chunks(paragraphs: List[str], target_len: int = TARGET_CH_LEN, overlap: int = OVERLAP_CH) -> List[str]:
    # Turn paragraphs → sentences → rolling chunks ~ target_len with overlap
    chunks = []
    buf = ""
    for para in paragraphs:
        for s in split_into_sentences(para):
            if len(buf) + 1 + len(s) <= target_len:
                buf = (buf + " " + s).strip() if buf else s
            else:
                if buf:
                    chunks.append(buf)
                # start next with overlap from previous buffer end
                if overlap > 0 and len(buf) > overlap:
                    carry = buf[-overlap:]
                    buf = (carry + " " + s).strip()
                else:
                    buf = s
    if buf:
        chunks.append(buf)
    # de‑dup tiny stragglers
    chunks = [c.strip() for c in chunks if len(c.strip()) >= 20]
    return chunks

def embed_texts(texts: List[str], model_name: str = MODEL_NAME, batch_size: int = 64) -> np.ndarray:
    model = SentenceTransformer(model_name)
    vecs = model.encode(texts, batch_size=batch_size, convert_to_numpy=True, show_progress_bar=len(texts) > 128, normalize_embeddings=True)
    return vecs.astype(np.float32)

def build_faiss_index(vecs: np.ndarray) -> faiss.Index:
    d = vecs.shape[1]
    index = faiss.IndexFlatIP(d)  # cosine sim since normalized
    index.add(vecs)
    return index

def greedy_farthest_pivots(vecs: np.ndarray, max_pivots: int) -> List[int]:
    # Farthest‑point sampling in cosine distance (1 - sim)
    n = vecs.shape[0]
    if n == 0:
        return []
    max_pivots = min(max_pivots, max(1, n))
    # start with the most "average‑distant" point
    centroid = vecs.mean(axis=0, keepdims=True)
    sims = (vecs @ centroid.T).ravel()
    seed = int(np.argmin(sims))  # least similar to centroid
    pivots = [seed]
    # distance to nearest pivot for each point
    nearest_sim = vecs @ vecs[seed]
    for _ in range(1, max_pivots):
        # pick the point with minimum similarity to any pivot (i.e., farthest)
        pick = int(np.argmin(nearest_sim))
        if pick in pivots:
            # if repetition, choose a random remaining
            remaining = [i for i in range(n) if i not in pivots]
            if not remaining:
                break
            pick = int(np.random.choice(remaining))
        pivots.append(pick)
        # update nearest_sim
        cand = vecs @ vecs[pick]
        nearest_sim = np.maximum(nearest_sim, cand)
    return sorted(list(set(pivots)))

def assign_to_nearest_pivot(vecs: np.ndarray, pivots: List[int]) -> Tuple[np.ndarray, np.ndarray]:
    # Returns (assignments, distances) where distance = 1 - cosine_sim_to_pivot
    if len(pivots) == 0:
        return np.array([], dtype=int), np.array([], dtype=np.float32)
    P = vecs[pivots]  # (p, d)
    sims = vecs @ P.T  # (n, p)
    best = sims.argmax(axis=1)
    best_sim = sims[np.arange(vecs.shape[0]), best]
    dist = 1.0 - best_sim
    return best.astype(int), dist.astype(np.float32)

def compute_bands_for_pivot(dists: np.ndarray, quantiles: List[float]) -> np.ndarray:
    # Given distances for items assigned to a single pivot, return per‑item band in {0..len(quantiles)}
    if dists.size == 0:
        return np.array([], dtype=int)
    qs = np.quantile(dists, quantiles).tolist()
    def band_of(x):
        for b, q in enumerate(qs):
            if x <= q: 
                return b
        return len(qs)
    return np.array([band_of(x) for x in dists], dtype=int)

def knn_redundancy(vecs: np.ndarray, k: int = KNN) -> np.ndarray:
    # Mean similarity to k nearest neighbors (excluding self). Higher = more redundant.
    idx = build_faiss_index(vecs)
    k_eff = min(k+1, vecs.shape[0])
    sims, inds = idx.search(vecs, k_eff)  # includes self at rank 0
    sims = sims[:, 1:] if sims.shape[1] > 1 else sims  # drop self
    if sims.size == 0:
        return np.zeros(vecs.shape[0], dtype=np.float32)
    # sims are dot products (cosine), average them
    return sims.mean(axis=1).astype(np.float32)

def minmax_norm(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    lo, hi = float(np.min(x)), float(np.max(x))
    if hi - lo < 1e-8:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)

# ======= Main Pipeline =======
def process_file(file_path):
    # --- Upload .docx (Colab) or read local file variable DOCX_PATH
    filename = os.path.basename(file_path)
    print(f"Processing {filename}")
    os.mkdir(filename)
    
    if not os.path.exists(file_path):
        raise RuntimeError("Set DOCX_PATH env var to a .docx file path when not in Colab.")

    # --- Parse and chunk
    paragraphs = read_file(file_path)
    if not paragraphs:
        raise RuntimeError("No paragraphs found. Is the .docx valid/non‑empty?")
    chunks = make_chunks(paragraphs, TARGET_CH_LEN, OVERLAP_CH)
    print(f"Parsed {len(paragraphs)} paragraphs → {len(chunks)} chunks.")

    # --- Embed
    vecs = embed_texts(chunks, MODEL_NAME)
    n = vecs.shape[0]

    # --- Pivots via farthest‑point sampling
    pivots = greedy_farthest_pivots(vecs, max_pivots=min(NUM_PIVOTS, max(1, int(np.ceil(np.sqrt(n))))))
    print(f"Selected {len(pivots)} pivots.")

    # --- Assign each chunk to nearest pivot; compute novelty (distance to pivot)
    assign_idx, novelty_dist = assign_to_nearest_pivot(vecs, pivots)

    # --- Redundancy via mean similarity to K nearest neighbors
    redundancy_sim = knn_redundancy(vecs, KNN)

    # Normalize novelty (distance) high→novel, and redundancy high→redundant
    novelty_norm    = minmax_norm(novelty_dist)  # 0..1
    redundancy_norm = minmax_norm(redundancy_sim)  # 0..1

    # Energy: novelty * (1 - redundancy)
    energy = (novelty_norm * (1.0 - redundancy_norm)).astype(np.float32)

    # --- Build bands per pivot
    band_ids = np.zeros(n, dtype=int)
    for p_local, p in enumerate(pivots):
        mask = (assign_idx == p_local)
        pdists = novelty_dist[mask]
        pbands = compute_bands_for_pivot(pdists, BAND_QUANTILES)
        band_ids[mask] = pbands

    # --- Prepare outputs
    pivot_texts = [chunks[i] for i in pivots]
    records = []
    for i, text in enumerate(chunks):
        pid_local = int(assign_idx[i])
        pid_global = int(pivots[pid_local])
        rec = {
            "id": i,
            "text": text,
            "pivot_id": pid_global,
            "pivot_local_index": pid_local,
            "pivot_text": chunks[pid_global],
            "band": int(band_ids[i]),
            "novelty": float(novelty_norm[i]),
            "redundancy": float(redundancy_norm[i]),
            "energy": float(energy[i]),
            "source": os.path.basename(file_path),
        }
        records.append(rec)

    # DataFrames
    df = pd.DataFrame(records).sort_values(["pivot_local_index", "band", "energy"], ascending=[True, True, False])
    piv_df = pd.DataFrame({"pivot_id": pivots, "pivot_text": pivot_texts})

    # --- Save files
    out_jsonl = os.path.join(filename, "structured_dataset.jsonl")
    out_piv   = os.path.join(filename, "pivots.json")
    out_csv   = os.path.join(filename, "train.csv")
    out_card  = os.path.join(filename, "dataset_card.md")

    with open(out_jsonl, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(out_piv, "w", encoding="utf-8") as f:
        json.dump({"pivots": [{"pivot_id": int(i), "text": t} for i, t in zip(pivots, pivot_texts)]}, f, ensure_ascii=False, indent=2)
    df_csv = df[["text", "pivot_local_index"]].rename(columns={"pivot_local_index": "label"})
    df_csv.to_csv(out_csv, index=False)

    with open(out_card, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(f'''
        # Dataset Card — Pivot‑Banded Structuring (Prototype)

        **Source doc:** `{os.path.basename(file_path)}`
        **Chunks:** {n}
        **Pivots:** {len(pivots)}
        **Bands:** {len(BAND_QUANTILES)+1} per pivot

        ## Files
        - `structured_dataset.jsonl`: One JSON object per chunk with fields:
          - `id`, `text`
          - `pivot_id`, `pivot_local_index`, `pivot_text`
          - `band` (0 = closest to pivot)
          - `novelty` in [0,1], `redundancy` in [0,1], `energy` = novelty * (1 - redundancy)
          - `source`
        - `pivots.json`: Pivot list with their texts.
        - `train.csv`: Simple (text, label) pairs where `label` is `pivot_local_index` for quick prototyping.

        ## Suggested Uses
        - **Curriculum training:** sample band 0 first, then 1..k
        - **Active labeling:** label only `pivots` and propagate to their bands
        - **Dedup:** filter chunks with high `redundancy`
        - **High‑value mining:** prioritize top `energy` chunks per pivot

        ## Notes
        - Embeddings: `{MODEL_NAME}` normalized for cosine similarity.
        - Neighborhoods use FAISS (inner product) with K={KNN}.
        - Pivots via greedy farthest‑point sampling.
        - Bands by distance quantiles: {BAND_QUANTILES + [1.0]}.

        ## Repro / Tuning
        - Adjust `NUM_PIVOTS`, `TARGET_CH_LEN`, `KNN`, and `BAND_QUANTILES` at the top.
        - For very large docs, consider IVF/PQ FAISS indexes (not included here).
        ''').strip())

    # --- Simple plot: band sizes (matplotlib, single plot, default colors)
    band_counts = df.groupby("band").size().sort_index()
    plt.figure()
    band_counts.plot(kind="bar")
    plt.title("Band Sizes")
    plt.xlabel("Band")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(os.path.join(filename, "band_sizes.png"), dpi=150)
    plt.close()

    # --- Preview
    print("\n==== Pivots (first 5) ====")
    for i, (pid, ptxt) in enumerate(zip(pivots[:5], pivot_texts[:5])):
        sub_ptxt = ptxt[:120].replace('\n',' ')
        print(f"[pivot {i}] chunk#{pid}: {sub_ptxt}{'...' if len(ptxt)>120 else ''}")
    print("\n==== Sample rows (top 10 by energy) ====")
    disp = df.sort_values("energy", ascending=False).head(10)
    print(disp[["id","pivot_local_index","band","energy"]].to_string(index=False))
    print("\nSaved files: structured_dataset.jsonl, pivots.json, train.csv, dataset_card.md, band_sizes.png")

def run_cli():
    fire.Fire(process_file)

if __name__ == "__main__":
    run_cli
    