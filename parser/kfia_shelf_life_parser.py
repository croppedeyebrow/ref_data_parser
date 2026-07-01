#!/usr/bin/env python3
"""
KFIA 식품유형별 소비기한 설정 보고서 PDF 파서
============================================
사용법:
    python kfia_shelf_life_parser.py [옵션] <PDF파일 또는 폴더>

예시:
    python kfia_shelf_life_parser.py 17__식육가공품.pdf
    python kfia_shelf_life_parser.py ./pdfs/
    python kfia_shelf_life_parser.py ./pdfs/ -o all.csv --db shelf_life.db

의존성:
    pip install pypdf tqdm
"""

import re
import csv
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

try:
    import pypdf
except ImportError:
    print("[오류] pypdf가 설치되지 않았습니다.\n  pip install pypdf", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ---------------------------------------------
# 상수 / 패턴
# ---------------------------------------------

# KFIA PDF 유니코드 좌우 따옴표
QUOTE = r"[\u2018\u2019'\u201c\u201d]"
CODE_PAT = re.compile(QUOTE + r"(\d+-\d+-\d+-\d+)" + QUOTE)

CSV_HEADERS = [
    "품목코드",
    "식품유형",
    "성상",
    "포장방법",
    "기존유통기한",
    "보존유통온도",
    # 대표값 (기준 실험온도 기준)
    "보관방법",
    "기준온도",
    "품질안전한계기간_일",
    "안전계수",
    "소비기한참고값_일",
    # 온도별 상세
    "온도별_상세_json",
    # 메타
    "source_pdf",
    "source_page",
    "추출일시",
]


# ---------------------------------------------
# 헬퍼
# ---------------------------------------------

def extract_field(label: str, text: str) -> str:
    m = re.search(label + r"[ \t]+(.+)", text)
    return (m.group(1).strip().replace(",", "/") if m else "")


def parse_days(s: str):
    """'70일', '70일c', '-d' 등에서 정수 추출. 없으면 None."""
    m = re.match(r"(\d+)일", s.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------
# 제품기본정보 페이지
# ---------------------------------------------

def parse_product_page(text: str):
    """
    반환: (품목코드, info_dict) or (None, None)
    식별: "17-X-X-X)" + "식품유형" + "보존 및 유통온도" 동시 존재
    """
    if not ("식품유형" in text and "보존 및 유통온도" in text):
        return None, None

    code_m = re.search(r"(\d+-\d+-\d+-\d+)\)", text)
    if not code_m:
        return None, None
    code = code_m.group(1)

    return code, {
        "식품유형":     extract_field("식품유형", text),
        "성상":         extract_field("성상", text),
        "포장방법":     extract_field(r"포장 방법", text),
        "기존유통기한": extract_field(r"유통기한\(기존\)", text),
        "보존유통온도": extract_field(r"보존 및 유통온도", text),
    }


# ---------------------------------------------
# 소비기한 결론 페이지
# ---------------------------------------------

def parse_shelf_life_page(text: str, page_num: int):
    """
    반환: (품목코드, sl_dict) or (None, None)

    온도별 상세 파싱 전략:
      pdfplumber 출력에서 "이화학지표b  70일c  70일  0.77  53일" 라인에
      한계기간/안전계수/소비기한이 모두 들어있음.
      온도는 바로 위에 "10℃" / "5℃" / "-18℃" 라인으로 분리.
      보관방법은 온도 아래 "(냉장)" / "(냉동)" / "(이탈온도)" 라인.
    """
    if "소비기한 참고값 설정" not in text:
        return None, None

    code_m = CODE_PAT.search(text)
    if not code_m:
        return None, None
    code = code_m.group(1)

    lines = text.split("\n")

    # -- 본문 요약값 (기준 실험온도 기준) -----------------
    lm = re.search(r"품질안전한계기간은\s*(\d+)일", text)
    sm = re.search(r"안전계수\s*([\d.]+)", text)
    rm = (
        re.search(r"소비기한 참고값은\s*(\d+)일", text)
        or re.search(r"최종 소비기한\s*(?:참고값[은]?\s*)?(\d+)일", text)
    )
    tm = re.search(r"냉([장동])\((-?\d+℃)\)", text)

    base_method = ("냉장" if tm.group(1) == "장" else "냉동") if tm else (
        "실온" if "실온" in text else ""
    )
    base_temp = tm.group(2) if tm else ""

    # -- 온도별 상세 표 파싱 ------------------------------
    # 핵심 라인 패턴: "이화학지표b  한계기간값  한계기간합산  안전계수  소비기한값"
    # e.g. "이화학지표b 70일c 70일 0.77 53일"
    #      "이화학지표b -d 52일 0.77 40일"
    DATA_LINE = re.compile(
        r"이화학지표b?\s+"        # 이화학지표 행
        r"(.+?)\s+"               # 각 지표별 한계기간 (70일c / -d 등)
        r"(\d+일[^\s]*)\s+"       # 품질안전한계기간 합산
        r"([\d.]+)\s+"            # 안전계수
        r"(\d+)일"                # 소비기한 참고값
    )

    # 온도 라인 인덱스 수집
    TEMP_LINE  = re.compile(r"^(-?\d+℃)$")
    METHOD_LINE = re.compile(r"^\((냉[장동]|이탈온도)\)$")

    temp_positions = []   # [(line_idx, 온도문자열, 보관방법)]
    for i, line in enumerate(lines):
        t_m = TEMP_LINE.match(line.strip())
        if t_m:
            temp_val = t_m.group(1)
            # 보관방법: 1~3줄 아래에서 찾기
            method = ""
            for j in range(i + 1, min(i + 4, len(lines))):
                mm = METHOD_LINE.match(lines[j].strip())
                if mm:
                    raw = mm.group(1)
                    method = "이탈온도" if raw == "이탈온도" else ("냉장" if "냉장" in raw else "냉동")
                    break
            temp_positions.append((i, temp_val, method))

    # 각 온도 블록에서 데이터 라인 매핑
    temp_details = []
    for ti, (t_idx, temp_val, method) in enumerate(temp_positions):
        # 다음 온도 전까지 블록 범위
        end_idx = temp_positions[ti + 1][0] if ti + 1 < len(temp_positions) else len(lines)
        block = "\n".join(lines[t_idx:end_idx])

        dm = DATA_LINE.search(block)
        if dm:
            limit_days  = parse_days(dm.group(2))
            safety_f    = float(dm.group(3))
            ref_days    = int(dm.group(4))
        else:
            # fallback: 숫자 3개 패턴 (한계기간 안전계수 소비기한)
            nums = re.findall(r"(\d+)일[^\s]*\s+([\d.]+)\s+(\d+)일", block)
            if nums:
                limit_days = int(nums[0][0])
                safety_f   = float(nums[0][1])
                ref_days   = int(nums[0][2])
            else:
                limit_days = safety_f = ref_days = None

        temp_details.append({
            "온도":           temp_val,
            "보관방법":       method,
            "품질안전한계기간": limit_days,
            "안전계수":       safety_f,
            "소비기한참고값": ref_days,
        })

    return code, {
        "보관방법":              base_method,
        "기준온도":              base_temp,
        "품질안전한계기간_일":   int(lm.group(1)) if lm else None,
        "안전계수":              float(sm.group(1)) if sm else None,
        "소비기한참고값_일":     int(rm.group(1)) if rm else None,
        "온도별_상세":           temp_details,
        "source_page":           page_num,
    }


# ---------------------------------------------
# PDF 1개 처리
# ---------------------------------------------

def process_pdf(pdf_path: Path) -> list[dict]:
    product_info: dict[str, dict] = {}
    shelf_info:   dict[str, dict] = {}

    try:
        reader = pypdf.PdfReader(str(pdf_path))
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as e:
                print(f"  [경고] {pdf_path.name} p{i} 스킵: {e}", file=sys.stderr)
                continue

            code, info = parse_product_page(text)
            if code and code not in product_info:
                product_info[code] = info

            code, sl = parse_shelf_life_page(text, i)
            if code and code not in shelf_info:
                shelf_info[code] = sl

    except Exception as e:
        print(f"  [오류] {pdf_path.name}: {e}", file=sys.stderr)
        return []

    extracted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for code in sorted(shelf_info.keys()):
        sl   = shelf_info[code]
        info = product_info.get(code, {})
        records.append({
            "품목코드":              code,
            "식품유형":              info.get("식품유형", ""),
            "성상":                  info.get("성상", ""),
            "포장방법":              info.get("포장방법", ""),
            "기존유통기한":          info.get("기존유통기한", ""),
            "보존유통온도":          info.get("보존유통온도", ""),
            "보관방법":              sl.get("보관방법", ""),
            "기준온도":              sl.get("기준온도", ""),
            "품질안전한계기간_일":   sl.get("품질안전한계기간_일"),
            "안전계수":              sl.get("안전계수"),
            "소비기한참고값_일":     sl.get("소비기한참고값_일"),
            "온도별_상세_json":      json.dumps(sl.get("온도별_상세", []), ensure_ascii=False),
            "source_pdf":            pdf_path.name,
            "source_page":           sl.get("source_page"),
            "추출일시":              extracted_at,
        })
    return records


# ---------------------------------------------
# 저장
# ---------------------------------------------

def save_csv(records: list[dict], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(records)
    print(f"[완료] CSV  -> {path}  ({len(records)}건)")


def save_sqlite(records: list[dict], path: Path):
    conn = sqlite3.connect(path)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shelf_life (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            품목코드                TEXT NOT NULL,
            식품유형                TEXT,
            성상                    TEXT,
            포장방법                TEXT,
            기존유통기한            TEXT,
            보존유통온도            TEXT,
            보관방법                TEXT,
            기준온도                TEXT,
            품질안전한계기간_일     INTEGER,
            안전계수                REAL,
            소비기한참고값_일       INTEGER,
            온도별_상세_json        TEXT,
            source_pdf              TEXT,
            source_page             INTEGER,
            추출일시                TEXT,
            UNIQUE(품목코드, source_pdf)
        )
    """)
    inserted = skipped = 0
    for r in records:
        cur.execute("""
            INSERT OR IGNORE INTO shelf_life VALUES
            (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [r[h] for h in CSV_HEADERS])
        if cur.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    conn.close()
    print(f"[완료] DB   -> {path}  (신규 {inserted}건 / 중복 스킵 {skipped}건)")


# ---------------------------------------------
# 요약 출력
# ---------------------------------------------

def print_summary(records: list[dict]):
    if not records:
        print("\n[결과] 추출된 데이터 없음")
        return

    from collections import Counter
    print(f"\n{'-'*55}")
    print(f"  총 추출: {len(records)}건")

    fc = Counter(r["식품유형"] for r in records if r["식품유형"])
    print(f"\n  식품유형별:")
    for ft, n in fc.most_common():
        print(f"    {ft:12} {n}건")

    mc = Counter(r["보관방법"] for r in records)
    print(f"\n  보관방법별:")
    for m, n in mc.most_common():
        print(f"    {m or '(미확인)':8} {n}건")

    days = [r["소비기한참고값_일"] for r in records if r["소비기한참고값_일"]]
    if days:
        print(f"\n  소비기한 범위: {min(days)}일 ~ {max(days)}일  (평균 {sum(days)//len(days)}일)")

    missing = [r["품목코드"] for r in records if not r["소비기한참고값_일"]]
    if missing:
        print(f"\n  [주의] 소비기한 누락: {', '.join(missing)}")

    # 온도별 상세 파싱 성공률
    detail_ok = sum(
        1 for r in records
        if json.loads(r["온도별_상세_json"] or "[]")
    )
    print(f"\n  온도별 상세 파싱: {detail_ok}/{len(records)}건 성공")
    print(f"{'-'*55}\n")


# ---------------------------------------------
# CLI
# ---------------------------------------------

def collect_pdfs(inputs):
    pdfs = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            pdfs.extend(sorted(p.glob("**/*.pdf")))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            pdfs.append(p)
        else:
            print(f"[경고] 찾을 수 없음: {inp}", file=sys.stderr)
    return pdfs


def main():
    parser = argparse.ArgumentParser(
        description="KFIA 소비기한 PDF -> CSV/SQLite 파서",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="+", help="PDF 파일 또는 폴더")
    parser.add_argument("-o", "--output", default="shelf_life_output.csv")
    parser.add_argument("--db",      default=None, help="SQLite DB 경로")
    parser.add_argument("--json",    default=None, dest="json_out")
    parser.add_argument("--no-csv",  action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    pdfs = collect_pdfs(args.input)
    if not pdfs:
        print("[오류] PDF 없음", file=sys.stderr)
        sys.exit(1)

    print(f"\n[시작] 처리 대상: {len(pdfs)}개 PDF\n")

    all_records = []
    it = tqdm(pdfs, unit="pdf") if (HAS_TQDM and not args.verbose) else pdfs

    for pdf_path in it:
        if args.verbose:
            print(f"처리 중: {pdf_path.name}")
        recs = process_pdf(pdf_path)
        all_records.extend(recs)
        if args.verbose:
            print(f"  -> {len(recs)}건")

    print_summary(all_records)
    if not all_records:
        sys.exit(0)

    if not args.no_csv:
        save_csv(all_records, Path(args.output))
    if args.db:
        save_sqlite(all_records, Path(args.db))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(all_records, f, ensure_ascii=False, indent=2)
        print(f"[완료] JSON -> {args.json_out}")


if __name__ == "__main__":
    main()
