#!/usr/bin/env python3
"""폴더 안의 파일 내용을 Gemini(무료 API)로 읽고 알맞은 이름을 제안해서 바꿔 주는 도구.

기본은 '미리보기'만 보여주고, --apply 를 붙여야 실제로 이름을 바꿉니다.
바꾼 기록은 폴더 안 .rename_log.json 에 남아서 --undo 로 되돌릴 수 있습니다.

사용법:
    export GEMINI_API_KEY="발급받은_키"      # https://aistudio.google.com/apikey
    python3 ai_rename.py ~/Downloads/문서      # 미리보기
    python3 ai_rename.py ~/Downloads/문서 --apply
    python3 ai_rename.py ~/Downloads/문서 --undo
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-2.5-flash-lite"  # 무료 한도가 가장 넉넉한 편. 바꾸려면 --model
LOG_NAME = ".rename_log.json"
MAX_INLINE_BYTES = 10 * 1024 * 1024  # 이미지/PDF는 10MB까지만 보냄

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".htm",
    ".css", ".js", ".ts", ".py", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh",
    ".yml", ".yaml", ".toml", ".ini", ".log", ".srt", ".vtt", ".tex", ".rtf",
}
MEDIA_MIME = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".gif": "image/gif",
}

PROMPT = (
    "다음 파일의 내용을 보고, 내용을 가장 잘 나타내는 짧은 파일 이름을 지어 주세요.\n"
    "규칙:\n"
    "- 확장자는 쓰지 말 것.\n"
    "- {lang}로, 2~6개 단어, 최대 {maxlen}자.\n"
    "- 명사 중심으로 구체적으로. '파일', '문서', '이미지' 같은 일반어만 쓰지 말 것.\n"
    "- 슬래시, 따옴표 등 파일 이름에 못 쓰는 문자는 쓰지 말 것.\n"
    "- 내용이 없거나 판단할 수 없으면 name 을 빈 문자열로.\n"
    '반드시 {{"name": "..."}} 형태의 JSON 으로만 답하세요.\n'
    "현재 파일 이름(참고용): {orig}"
)


def read_part(path, max_chars):
    """파일 내용을 Gemini 요청의 part 로 변환. 읽을 수 없으면 None."""
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    if ext in MEDIA_MIME:
        if size > MAX_INLINE_BYTES:
            return None
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"inline_data": {"mime_type": MEDIA_MIME[ext], "data": data}}
    if ext in TEXT_EXTS or (mimetypes.guess_type(path)[0] or "").startswith("text/"):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(max_chars).strip()
        return {"text": "[파일 내용]\n" + text} if text else None
    return None


def ask_gemini(parts, api_key, model, retries=4):
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.3,
        },
    }).encode()
    req = urllib.request.Request(
        API_URL.format(model=model), data=body, method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    wait = 8
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.load(resp)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text).get("name", "")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(wait)  # 무료 한도(분당 요청 수) 초과 시 잠시 쉬었다 재시도
                wait *= 2
                continue
            detail = e.read().decode(errors="ignore")[:300]
            raise RuntimeError(f"API 오류 {e.code}: {detail}")
        except (KeyError, IndexError, json.JSONDecodeError):
            return ""  # 응답이 막혔거나 형식이 달라도 전체 작업은 계속
    return ""


def clean_name(name, maxlen):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:maxlen].strip(" .")


def unique_path(folder, base, ext, taken):
    """같은 이름이 이미 있거나 이번에 쓰기로 한 이름이면 _2, _3 ... 을 붙임."""
    n, cand = 1, base + ext
    while cand.lower() in taken or os.path.exists(os.path.join(folder, cand)):
        n += 1
        cand = f"{base}_{n}{ext}"
    return cand


def plan(folder, args, api_key):
    files = sorted(
        f for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and not f.startswith(".")
    )
    taken = {f.lower() for f in files}
    rows = []
    for i, fn in enumerate(files, 1):
        path = os.path.join(folder, fn)
        ext = os.path.splitext(fn)[1]
        print(f"[{i}/{len(files)}] {fn} ...", end=" ", flush=True)
        part = read_part(path, args.max_chars)
        if part is None:
            print("건너뜀 (읽을 수 없는 형식/너무 큼/비어 있음)")
            rows.append((fn, None, "읽을 수 없음"))
            continue
        prompt = PROMPT.format(lang=args.lang, maxlen=args.max_len, orig=fn)
        new = clean_name(ask_gemini([{"text": prompt}, part], api_key, args.model), args.max_len)
        if i < len(files):
            time.sleep(args.delay)
        if not new:
            print("건너뜀 (제안 없음)")
            rows.append((fn, None, "제안 없음"))
            continue
        taken.discard(fn.lower())  # 자기 자신의 옛 이름은 비워 둠
        target = unique_path(folder, new, ext, taken)
        taken.add(target.lower())
        print("->", target)
        rows.append((fn, target, ""))
    return rows


def apply_rows(folder, rows):
    done = []
    for old, new, _ in rows:
        if not new or old == new:
            continue
        src, dst = os.path.join(folder, old), os.path.join(folder, new)
        if os.path.exists(dst):  # 덮어쓰기 방지
            print(f"건너뜀(이미 존재): {new}")
            continue
        os.rename(src, dst)
        done.append({"old": old, "new": new})
    if done:
        log_path = os.path.join(folder, LOG_NAME)
        history = []
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as f:
                history = json.load(f)
        history.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "renames": done})
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    return len(done)


def undo(folder):
    log_path = os.path.join(folder, LOG_NAME)
    if not os.path.exists(log_path):
        sys.exit("되돌릴 기록(.rename_log.json)이 없어요.")
    with open(log_path, encoding="utf-8") as f:
        history = json.load(f)
    if not history:
        sys.exit("되돌릴 기록이 없어요.")
    last = history.pop()
    count = 0
    for r in reversed(last["renames"]):
        src, dst = os.path.join(folder, r["new"]), os.path.join(folder, r["old"])
        if os.path.exists(src) and not os.path.exists(dst):
            os.rename(src, dst)
            count += 1
        else:
            print(f"건너뜀: {r['new']} -> {r['old']}")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"{count}개 파일을 원래 이름으로 되돌렸어요.")


def main():
    ap = argparse.ArgumentParser(description="Gemini 로 파일 내용을 읽고 이름을 다시 지어 줍니다.")
    ap.add_argument("folder", help="이름을 바꿀 파일이 있는 폴더")
    ap.add_argument("--apply", action="store_true", help="미리보기 후 실제로 이름 바꾸기")
    ap.add_argument("--yes", action="store_true", help="--apply 시 확인 질문 생략")
    ap.add_argument("--undo", action="store_true", help="가장 최근 이름 변경을 되돌리기")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Gemini 모델 (기본 {DEFAULT_MODEL})")
    ap.add_argument("--lang", default="한국어", help="이름 언어 (기본 한국어)")
    ap.add_argument("--max-len", type=int, default=40, help="이름 최대 글자 수")
    ap.add_argument("--max-chars", type=int, default=6000, help="텍스트 파일에서 읽을 글자 수")
    ap.add_argument("--delay", type=float, default=4.5, help="요청 사이 대기 초 (무료 한도 보호)")
    args = ap.parse_args()

    folder = os.path.abspath(os.path.expanduser(args.folder))
    if not os.path.isdir(folder):
        sys.exit(f"폴더를 찾을 수 없어요: {folder}")
    if args.undo:
        return undo(folder)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY 환경변수가 없어요. https://aistudio.google.com/apikey 에서 무료 키를 만드세요.")

    print("주의: 파일 내용이 Google Gemini 로 전송됩니다. 무료 등급은 입력이 모델 개선에 쓰일 수 있어요.\n"
          "민감한 파일이 있는 폴더라면 중단하세요 (Ctrl+C).\n")
    rows = plan(folder, args, api_key)

    changes = [(o, n) for o, n, _ in rows if n and o != n]
    print(f"\n=== 미리보기: {len(changes)}개 변경 / 전체 {len(rows)}개 ===")
    for o, n in changes:
        print(f"  {o}\n    -> {n}")
    if not changes:
        return
    if not args.apply:
        print("\n실제로 바꾸려면 같은 명령에 --apply 를 붙이세요.")
        return
    if not args.yes and input("\n위 내용대로 이름을 바꿀까요? [y/N] ").strip().lower() != "y":
        print("취소했어요.")
        return
    n = apply_rows(folder, rows)
    print(f"{n}개 파일 이름을 바꿨어요. 되돌리기: python3 {os.path.basename(sys.argv[0])} \"{folder}\" --undo")


if __name__ == "__main__":
    main()
