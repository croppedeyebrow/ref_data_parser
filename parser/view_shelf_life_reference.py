"""
shelf_life_reference.csv → HTML 표 뷰어 생성

사용법:
    python parser/view_shelf_life_reference.py
    python parser/view_shelf_life_reference.py --input output/shelf_life_reference.csv
"""

import argparse
import html
from pathlib import Path

import pandas as pd

LEVEL_CLASS = {
    "fine": "level-fine",
    "coarse": "level-coarse",
    "low_confidence": "level-low",
}


def build_html(df: pd.DataFrame, title: str) -> str:
    headers = "".join(f"<th>{html.escape(c)}</th>" for c in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in df.columns:
            val = row[col]
            text = "" if pd.isna(val) else html.escape(str(val))
            if col == "source_level" and text in LEVEL_CLASS:
                cells.append(f'<td><span class="badge {LEVEL_CLASS[text]}">{text}</span></td>')
            elif col in ("median_days", "conservative_days", "min_days", "max_days"):
                cells.append(f'<td class="num">{text}</td>')
            elif col == "sample_count":
                cells.append(f'<td class="num int">{text}</td>')
            else:
                cells.append(f"<td>{text}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: "Segoe UI", "Malgun Gothic", sans-serif; margin: 0; padding: 1.5rem; background: #f5f6f8; color: #1a1a1a; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem; }}
    .meta {{ color: #666; font-size: 0.875rem; margin-bottom: 1rem; }}
    .toolbar {{ display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }}
    #search {{ padding: 0.5rem 0.75rem; border: 1px solid #ccc; border-radius: 6px; min-width: 220px; font-size: 0.9rem; }}
    #levelFilter {{ padding: 0.5rem; border-radius: 6px; border: 1px solid #ccc; }}
    .wrap {{ overflow: auto; max-height: calc(100vh - 140px); border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); background: #fff; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.8125rem; }}
    thead {{ position: sticky; top: 0; z-index: 1; }}
    th {{ background: #2c3e50; color: #fff; padding: 0.6rem 0.75rem; text-align: left; white-space: nowrap; cursor: pointer; user-select: none; }}
    th:hover {{ background: #34495e; }}
    td {{ padding: 0.45rem 0.75rem; border-bottom: 1px solid #eee; max-width: 280px; }}
    tr:hover td {{ background: #f0f7ff; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.int {{ font-weight: 600; }}
    .badge {{ display: inline-block; padding: 0.15rem 0.45rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }}
    .level-fine {{ background: #d4edda; color: #155724; }}
    .level-coarse {{ background: #fff3cd; color: #856404; }}
    .level-low {{ background: #f8d7da; color: #721c24; }}
    .hidden {{ display: none; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p class="meta">{len(df)} rows × {len(df.columns)} columns</p>
  <div class="toolbar">
    <input type="search" id="search" placeholder="검색 (모든 컬럼)…" autofocus>
    <select id="levelFilter">
      <option value="">source_level: 전체</option>
      <option value="fine">fine</option>
      <option value="coarse">coarse</option>
      <option value="low_confidence">low_confidence</option>
    </select>
  </div>
  <div class="wrap">
    <table id="tbl">
      <thead><tr>{headers}</tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
  <script>
    const search = document.getElementById('search');
    const levelFilter = document.getElementById('levelFilter');
    const tbody = document.querySelector('#tbl tbody');

    function applyFilters() {{
      const q = search.value.toLowerCase();
      const lv = levelFilter.value;
      for (const tr of tbody.rows) {{
        const text = tr.textContent.toLowerCase();
        const level = tr.cells[8]?.textContent.trim();
        const okSearch = !q || text.includes(q);
        const okLevel = !lv || level === lv;
        tr.classList.toggle('hidden', !(okSearch && okLevel));
      }}
    }}
    search.addEventListener('input', applyFilters);
    levelFilter.addEventListener('change', applyFilters);

    document.querySelectorAll('th').forEach((th, i) => {{
      th.addEventListener('click', () => {{
        const rows = [...tbody.rows].filter(r => !r.classList.contains('hidden'));
        const asc = th.dataset.asc !== 'true';
        rows.sort((a, b) => {{
          let av = a.cells[i].textContent.trim();
          let bv = b.cells[i].textContent.trim();
          const an = parseFloat(av), bn = parseFloat(bv);
          if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
          return asc ? av.localeCompare(bv, 'ko') : bv.localeCompare(av, 'ko');
        }});
        rows.forEach(r => tbody.appendChild(r));
        document.querySelectorAll('th').forEach(h => delete h.dataset.asc);
        th.dataset.asc = asc;
      }});
    }});
  </script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser(description="shelf_life_reference.csv HTML 표 뷰")
    p.add_argument("--input", default="output/shelf_life_reference.csv")
    p.add_argument("--output", default="output/shelf_life_reference.html")
    args = p.parse_args()

    inp, out = Path(args.input), Path(args.output)
    df = pd.read_csv(inp)
    out.write_text(build_html(df, "Shelf Life Reference"), encoding="utf-8")
    print(f"[완료] {out}")
    print(f"  브라우저에서 열기: file:///{out.resolve().as_posix()}")


if __name__ == "__main__":
    main()
