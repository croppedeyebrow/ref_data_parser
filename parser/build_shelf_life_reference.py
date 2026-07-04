"""
NaengLog(냉장고 관리 앱) — 식약처/KFIA 소비기한 원본 CSV 집계 스크립트
====================================================================
kfia_shelf_life_parser.py 결과 CSV를 정제·집계하여 shelf_life_reference.csv를 생성한다.

사용법:
    python parser/build_shelf_life_reference.py
    python parser/build_shelf_life_reference.py --input output/shelf_life_output.csv
    python parser/build_shelf_life_reference.py --input output/shelf_life_output.csv \\
        --output output/shelf_life_reference.csv \\
        --min-sample 5 \\
        --plot output/shelf_life_sample_distribution.png
    python parser/build_shelf_life_reference.py --include-short-shelf-life

의존성:
    pip install pandas matplotlib
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

DEFAULT_MIN_SAMPLE = 5

FINE_KEYS = ["food_type", "storage_type", "package_type"]
COARSE_KEYS = ["food_type", "storage_type"]

OUTPUT_COLUMNS = [
    "food_type",
    "storage_type",
    "package_type",
    "sample_count",
    "median_days",
    "conservative_days",
    "min_days",
    "max_days",
    "source_level",
]


def preprocess(df: pd.DataFrame, include_short_shelf_life: bool) -> pd.DataFrame:
    """유효 행만 남기고, 시간 단위 환산·초단기 플래그를 적용한다."""
    out = df.copy()

    out["shelf_life_days"] = pd.to_numeric(out["소비기한참고값_일"], errors="coerce")
    hour_mask = out["단위"].fillna("").eq("시간")

    # 파서가 이미 일 단위로 환산했을 수 있음. 값이 크면(>10) 아직 '시간' 단위로 간주.
    unconverted = hour_mask & out["shelf_life_days"].gt(10)
    out.loc[unconverted, "shelf_life_days"] = out.loc[unconverted, "shelf_life_days"] / 24

    out["is_short_shelf_life"] = hour_mask & out["shelf_life_days"].lt(1.0)

    out = out.rename(
        columns={
            "식품유형": "food_type",
            "보관방법": "storage_type",
            "포장방법": "package_type",
        }
    )

    # null / 0 이하 제외
    out = out[out["shelf_life_days"].notna() & out["shelf_life_days"].gt(0)]

    # 집계 키가 비어 있으면 제외
    out = out[
        out["food_type"].fillna("").astype(str).str.strip().ne("")
        & out["storage_type"].fillna("").astype(str).str.strip().ne("")
        & out["package_type"].fillna("").astype(str).str.strip().ne("")
    ]

    if not include_short_shelf_life:
        out = out[~out["is_short_shelf_life"]]

    return out.reset_index(drop=True)


def aggregate_fine(df: pd.DataFrame) -> pd.DataFrame:
    """3컬럼(식품유형, 보관방법, 포장방법) 집계."""
    return (
        df.groupby(FINE_KEYS, dropna=False)["shelf_life_days"]
        .agg(
            sample_count="count",
            median_days="median",
            conservative_days=lambda s: s.quantile(0.25),
            min_days="min",
            max_days="max",
        )
        .reset_index()
    )


def aggregate_coarse(df: pd.DataFrame) -> pd.DataFrame:
    """2컬럼(식품유형, 보관방법) 집계."""
    return (
        df.groupby(COARSE_KEYS, dropna=False)["shelf_life_days"]
        .agg(
            sample_count="count",
            median_days="median",
            conservative_days=lambda s: s.quantile(0.25),
            min_days="min",
            max_days="max",
        )
        .reset_index()
    )


def merge_with_fallback(
    fine: pd.DataFrame,
    coarse: pd.DataFrame,
    min_sample: int,
) -> pd.DataFrame:
    """
    하이브리드 폴백:
    - 3컬럼 n >= min_sample → fine (package_type 유지)
    - 그 외 3컬럼 조합 → 해당 2컬럼 집계로 대체 (package_type=null)
    - 2컬럼 n < min_sample → low_confidence (값은 유지)
    """
    coarse_idx = coarse.set_index(COARSE_KEYS)
    rows: list[dict] = []

    for _, frow in fine.iterrows():
        key2 = (frow["food_type"], frow["storage_type"])
        if frow["sample_count"] >= min_sample:
            rows.append(
                {
                    "food_type": frow["food_type"],
                    "storage_type": frow["storage_type"],
                    "package_type": frow["package_type"],
                    "sample_count": int(frow["sample_count"]),
                    "median_days": round(frow["median_days"], 2),
                    "conservative_days": round(frow["conservative_days"], 2),
                    "min_days": round(frow["min_days"], 2),
                    "max_days": round(frow["max_days"], 2),
                    "source_level": "fine",
                }
            )
            continue

        if key2 not in coarse_idx.index:
            continue

        crow = coarse_idx.loc[key2]
        if isinstance(crow, pd.DataFrame):
            crow = crow.iloc[0]

        level = "coarse" if crow["sample_count"] >= min_sample else "low_confidence"
        rows.append(
            {
                "food_type": frow["food_type"],
                "storage_type": frow["storage_type"],
                "package_type": None,
                "sample_count": int(crow["sample_count"]),
                "median_days": round(crow["median_days"], 2),
                "conservative_days": round(crow["conservative_days"], 2),
                "min_days": round(crow["min_days"], 2),
                "max_days": round(crow["max_days"], 2),
                "source_level": level,
            }
        )

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if result.empty:
        return result

    # coarse 폴백 시 (food, storage, null) 행이 중복될 수 있어 dedupe
    result = result.drop_duplicates(
        subset=["food_type", "storage_type", "package_type", "source_level"],
        keep="first",
    )
    return result.sort_values(
        ["food_type", "storage_type", "package_type", "source_level"],
        na_position="last",
    ).reset_index(drop=True)


def save_outputs(
    reference: pd.DataFrame,
    csv_path: Path,
    plot_path: Path,
    min_sample: int,
) -> None:
    """CSV 저장, source_level 통계 출력, sample_count 히스토그램 저장."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    reference.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"[완료] CSV -> {csv_path}  ({len(reference)}행)")

    print("\n[source_level별 그룹 수]")
    counts = reference["source_level"].value_counts()
    for level in ["fine", "coarse", "low_confidence"]:
        print(f"  {level:16} {counts.get(level, 0):4}개")
    print(f"  {'합계':16} {len(reference):4}개")

    print(f"\n[sample_count 요약]  (MIN_SAMPLE={min_sample})")
    print(reference["sample_count"].describe().to_string())

    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(reference["sample_count"], bins=30, edgecolor="black", alpha=0.75)
    ax.axvline(min_sample, color="red", linestyle="--", linewidth=1.5, label=f"MIN_SAMPLE={min_sample}")
    ax.set_xlabel("sample_count")
    ax.set_ylabel("group count")
    ax.set_title("Shelf Life Reference — sample_count distribution")
    ax.legend()
    fig.tight_layout()
    fig.savefig(plot_path, dpi=120)
    plt.close(fig)
    print(f"[완료] Plot -> {plot_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="KFIA 소비기한 CSV → NaengLog 참조 테이블(shelf_life_reference.csv) 집계",
    )
    parser.add_argument(
        "--input",
        default="output/shelf_life_output.csv",
        help="전처리 대상 CSV (kfia_shelf_life_parser 결과)",
    )
    parser.add_argument(
        "--output",
        default="output/shelf_life_reference.csv",
        help="집계 결과 CSV 경로",
    )
    parser.add_argument(
        "--plot",
        default="output/shelf_life_sample_distribution.png",
        help="sample_count 히스토그램 PNG 경로",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=DEFAULT_MIN_SAMPLE,
        help="fine/coarse 신뢰 구분 최소 표본 수 (기본 5)",
    )
    parser.add_argument(
        "--include-short-shelf-life",
        action="store_true",
        help="24시간 미만(초단기) 품목도 집계에 포함 (기본: 제외)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"[오류] 입력 파일 없음: {input_path}", file=sys.stderr)
        sys.exit(1)

    raw = pd.read_csv(input_path)
    print(f"[입력] {input_path}  ({len(raw)}행)")

    cleaned = preprocess(raw, include_short_shelf_life=args.include_short_shelf_life)
    all_prep = preprocess(raw, include_short_shelf_life=True)
    short_flagged = int(all_prep["is_short_shelf_life"].sum())
    print(f"[전처리] 집계 대상 {len(cleaned)}행")
    if short_flagged and not args.include_short_shelf_life:
        print(f"  초단기 유통기한(<24h) 제외: {short_flagged}행")

    fine = aggregate_fine(cleaned)
    coarse = aggregate_coarse(cleaned)
    print(f"[집계] fine(3컬럼) {len(fine)}그룹, coarse(2컬럼) {len(coarse)}그룹")

    reference = merge_with_fallback(fine, coarse, min_sample=args.min_sample)
    save_outputs(
        reference,
        csv_path=Path(args.output),
        plot_path=Path(args.plot),
        min_sample=args.min_sample,
    )


if __name__ == "__main__":
    main()
