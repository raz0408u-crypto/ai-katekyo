import os
import json
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from google import genai

load_dotenv()

app = Flask(__name__)

DB_PATH = os.getenv("TUTOR_DB_PATH", "tutor.db")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
STUDENT_ID = "demo_student_001"

SYSTEM_PROMPT = """
あなたは高校生専用の個別指導AI教師です。

目的は、答えを一方的に教えることではなく、生徒が自分で理解して問題を解けるようにすることです。

【指導方針】
- 高校生にも分かる日本語で説明する。
- 生徒の現在の理解度に合わせる。
- いきなり答えを言わず、必要ならヒント→考え方→答えの順で支援する。
- 生徒が正解したら、短く褒めて次へ進む。
- 間違えたら責めず、どこで考え違いが起きたか一緒に確認する。
- 数学・英語・国語などでは、生徒が自力で考えるための問いかけを適切に入れる。
- 「分からない」と言われたら、難易度を下げて一段階ずつ説明する。
- 生徒の学習履歴を参考にして、以前の弱点を必要に応じて復習する。
- 生徒が求めた場合は、問題を一緒に解く。
- 学習上の重要な変化は、アプリ側の記録機能で保存される。AIが保存したと断言してはいけない。
- 個人情報やAPIキーなどの秘密情報を要求しない。
"""

def utc_now():
    return datetime.now(timezone.utc).isoformat()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS students (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        grade TEXT NOT NULL,
        goal TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        interaction_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS topic_mastery (
        student_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        level INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (student_id, subject, topic)
    );

    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        topic TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        correct INTEGER NOT NULL,
        feedback TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    existing = conn.execute(
        "SELECT id FROM students WHERE id = ?", (STUDENT_ID,)
    ).fetchone()

    if not existing:
        now = utc_now()
        conn.execute(
            """INSERT INTO students
               (id, name, grade, goal, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                STUDENT_ID,
                "生徒",
                "高校1年生",
                "自分に合った勉強方法を見つけて、少しずつ成績を上げる",
                "",
                now,
                now,
            ),
        )
    conn.commit()
    conn.close()

def get_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY がありません。.env にGemini APIキーを設定してください。"
        )
    return genai.Client(api_key=api_key)

def get_student():
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM students WHERE id = ?", (STUDENT_ID,)
    ).fetchone()
    conn.close()
    return dict(row)

def get_recent_messages(limit=20):
    conn = get_db()
    rows = conn.execute(
        """SELECT role, content, created_at
           FROM messages
           WHERE student_id = ?
           ORDER BY id DESC LIMIT ?""",
        (STUDENT_ID, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]

def get_mastery():
    conn = get_db()
    rows = conn.execute(
        """SELECT subject, topic, level, note, updated_at
           FROM topic_mastery
           WHERE student_id = ?
           ORDER BY updated_at DESC""",
        (STUDENT_ID,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_message(role, content):
    conn = get_db()
    conn.execute(
        """INSERT INTO messages(student_id, role, content, created_at)
           VALUES (?, ?, ?, ?)""",
        (STUDENT_ID, role, content, utc_now()),
    )
    conn.commit()
    conn.close()

def build_context():
    student = get_student()
    messages = get_recent_messages(20)
    mastery = get_mastery()

    return f"""
【生徒プロフィール】
名前: {student['name']}
学年: {student['grade']}
目標: {student['goal']}
メモ: {student['notes']}

【理解度記録】
{json.dumps(mastery, ensure_ascii=False, indent=2)}

【最近の会話】
{json.dumps(messages, ensure_ascii=False, indent=2)}
"""

def call_interaction(input_text, previous_interaction_id=None, system_instruction=SYSTEM_PROMPT):
    client = get_client()

    kwargs = {
        "model": MODEL,
        "system_instruction": system_instruction,
        "input": input_text,
        "generation_config": {
            "temperature": 0.7,
        },
    }

    if previous_interaction_id:
        kwargs["previous_interaction_id"] = previous_interaction_id

    interaction = client.interactions.create(**kwargs)

    text = (interaction.output_text or "").strip()
    if not text:
        raise RuntimeError("Geminiからテキスト回答が返りませんでした。")

    return interaction.id, text

def parse_json(text):
    text = text.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise

def generate_problem(subject, topic, difficulty):
    prompt = f"""
高校生向けの練習問題を1問作ってください。

教科: {subject}
単元: {topic}
難易度: {difficulty}

次のJSONだけを返してください。説明文やMarkdownは不要です。

{{
  "question": "問題文",
  "answer": "正解",
  "explanation": "正解に至る考え方を高校生向けに短く説明",
  "hint": "最初に出すヒント"
}}
"""

    json_system = """
あなたは高校生向けの問題作成AIです。
必ず有効なJSONオブジェクトだけを返してください。
"""

    _, text = call_interaction(
        prompt,
        system_instruction=json_system,
    )
    data = parse_json(text)

    required = ["question", "answer", "explanation", "hint"]
    for key in required:
        if key not in data:
            raise ValueError(f"問題データに {key} がありません。")

    return data

def grade_answer(subject, topic, question, correct_answer, user_answer):
    prompt = f"""
高校生の問題回答を採点してください。

教科: {subject}
単元: {topic}
問題: {question}
正解: {correct_answer}
生徒の回答: {user_answer}

次のJSONだけを返してください。

{{
  "correct": true,
  "feedback": "生徒に伝える短いフィードバック",
  "reason": "なぜ正解/不正解なのか",
  "mastery_level": 0
}}

mastery_level は 0〜5 の整数です。
"""

    json_system = """
あなたは高校生向けの採点AIです。
生徒を責めず、理解につながるフィードバックをしてください。
必ず有効なJSONオブジェクトだけを返してください。
"""

    _, text = call_interaction(
        prompt,
        system_instruction=json_system,
    )
    return parse_json(text)

def update_mastery(subject, topic, level, note):
    level = max(0, min(5, int(level)))
    conn = get_db()
    conn.execute(
        """INSERT INTO topic_mastery(student_id, subject, topic, level, note, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(student_id, subject, topic)
           DO UPDATE SET
             level=excluded.level,
             note=excluded.note,
             updated_at=excluded.updated_at""",
        (STUDENT_ID, subject, topic, level, note, utc_now()),
    )
    conn.commit()
    conn.close()

@app.route("/")
def index():
    return render_template("index.html")

@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "model": MODEL,
        "message": "AI家庭教師サーバーは起動しています。",
    })

@app.get("/api/profile")
def profile():
    student = get_student()
    return jsonify({
        "student": student,
        "mastery": get_mastery(),
        "messages": get_recent_messages(30),
    })

@app.post("/api/chat")
def chat():
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get("message", "")).strip()

        if not user_message:
            return jsonify({"error": "メッセージを入力してください。"}), 400

        student = get_student()
        context = build_context()

        prompt = f"""
{context}

【今回の生徒の発言】
{user_message}

この発言に対して、個別指導教師として回答してください。
生徒が自分で考えられるように、必要なら質問やヒントを使ってください。
"""

        previous_id = student.get("interaction_id") or None

        save_message("user", user_message)

        interaction_id, answer = call_interaction(
            prompt,
            previous_interaction_id=previous_id,
        )

        save_message("assistant", answer)

        conn = get_db()
        conn.execute(
            "UPDATE students SET interaction_id = ?, updated_at = ? WHERE id = ?",
            (interaction_id, utc_now(), STUDENT_ID),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "answer": answer,
            "interaction_id": interaction_id,
        })

    except Exception as e:
        app.logger.exception("chat error")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

@app.post("/api/problem")
def problem():
    try:
        data = request.get_json(silent=True) or {}
        subject = str(data.get("subject", "国語")).strip()
        topic = str(data.get("topic", "現代文")).strip()
        difficulty = str(data.get("difficulty", "基礎")).strip()

        result = generate_problem(subject, topic, difficulty)

        conn = get_db()
        conn.execute(
            """INSERT INTO messages(student_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                STUDENT_ID,
                "system",
                f"問題生成: {subject}/{topic}/{difficulty}\n{result['question']}",
                utc_now(),
            ),
        )
        conn.commit()
        conn.close()

        return jsonify(result)

    except Exception as e:
        app.logger.exception("problem error")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

@app.post("/api/answer")
def answer():
    try:
        data = request.get_json(silent=True) or {}

        subject = str(data.get("subject", "国語")).strip()
        topic = str(data.get("topic", "現代文")).strip()
        question = str(data.get("question", "")).strip()
        correct_answer = str(data.get("correct_answer", "")).strip()
        user_answer = str(data.get("user_answer", "")).strip()

        if not all([question, correct_answer, user_answer]):
            return jsonify({"error": "採点に必要な情報が足りません。"}), 400

        result = grade_answer(
            subject,
            topic,
            question,
            correct_answer,
            user_answer,
        )

        correct = bool(result.get("correct", False))
        feedback = str(result.get("feedback", ""))
        level = int(result.get("mastery_level", 0))
        reason = str(result.get("reason", ""))

        update_mastery(subject, topic, level, feedback)

        conn = get_db()
        conn.execute(
            """INSERT INTO attempts
               (student_id, subject, topic, question, answer, correct, feedback, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                STUDENT_ID,
                subject,
                topic,
                question,
                user_answer,
                int(correct),
                f"{feedback}\n{reason}",
                utc_now(),
            ),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "correct": correct,
            "feedback": feedback,
            "reason": reason,
            "mastery_level": level,
        })

    except Exception as e:
        app.logger.exception("answer error")
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

init_db()

if __name__ == "__main__":
    print(f"使用モデル: {MODEL}")
    print("AI家庭教師サーバーを起動します。")
    app.run(debug=True, host="127.0.0.1", port=5000)
