
import os
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, session
from groq import Groq

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")


def get_student_id():
    if "student_id" not in session:
        session["student_id"] = f"student_{uuid.uuid4().hex}"
    return session["student_id"]


@app.before_request
def prepare_student():
    if request.endpoint == "static":
        return
    ensure_student()


DB_PATH = os.getenv("TUTOR_DB_PATH", "tutor.db")
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = """
あなたは高校生専用の個別指導AI教師です。

目的は、答えを一方的に教えることではなく、
生徒が自分で理解して問題を解けるようにすることです。

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
- 学習上の重要な変化は、アプリ側の記録機能で保存される。
  AIが保存したと断言してはいけない。
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
    conn.close()


def get_client():
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY がありません。.env にGroq APIキーを設定してください。"
        )
    return Groq(api_key=api_key)


def get_student():
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM students WHERE id = ?",
            (get_student_id(),)
        ).fetchone()

        if row is None:
            raise RuntimeError("生徒情報が見つかりません。")

        return dict(row)
    finally:
        conn.close()


def ensure_student():
    student_id = get_student_id()
    conn = get_db()

    try:
        row = conn.execute(
            "SELECT id FROM students WHERE id = ?",
            (student_id,)
        ).fetchone()

        if row is None:
            now = utc_now()
            conn.execute(
                """
                INSERT INTO students
                (id, name, grade, goal, notes, interaction_id,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student_id,
                    "生徒",
                    "高校1年生",
                    "自分に合った勉強方法を見つけて、少しずつ成績を上げる",
                    "",
                    "",
                    now,
                    now
                )
            )
            conn.commit()
    finally:
        conn.close()


def get_recent_messages(limit=20):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE student_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (get_student_id(), limit),
        ).fetchall()

        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def get_mastery():
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT subject, topic, level, note, updated_at
            FROM topic_mastery
            WHERE student_id = ?
            ORDER BY updated_at DESC
            """,
            (get_student_id(),)
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_attempts(limit=100):
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT subject, topic, question, answer,
                   correct, feedback, created_at
            FROM attempts
            WHERE student_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (get_student_id(), limit)
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_learning_suggestions():
    mastery = get_mastery()
    attempts = get_attempts(limit=100)

    suggestions = []

    # 理解度をもとに復習候補を作る
    for item in mastery:
        level = item.get("level", 0)
        subject = item.get("subject", "")
        topic = item.get("topic", "")

        if level <= 2:
            suggestions.append({
                "type": "review",
                "subject": subject,
                "topic": topic,
                "reason": f"理解度が{level}/5なので、基礎を復習しよう"
            })
        elif level == 3:
            suggestions.append({
                "type": "practice",
                "subject": subject,
                "topic": topic,
                "reason": "例題を解いて理解を深めよう"
            })

    # 間違いが多い分野を探す
    mistake_counts = {}

    for item in attempts:
        if not item.get("correct"):
            key = (item["subject"], item["topic"])
            mistake_counts[key] = mistake_counts.get(key, 0) + 1

    for (subject, topic), count in mistake_counts.items():
        if count >= 2:
            suggestions.append({
                "type": "weakness",
                "subject": subject,
                "topic": topic,
                "reason": f"過去の記録で間違いが{count}回あるため、復習がおすすめ"
            })

    # 同じ分野のおすすめが重複しないようにする
    unique = {}
    for item in suggestions:
        key = (item["subject"], item["topic"])
        if key not in unique or item["type"] == "weakness":
            unique[key] = item

    return list(unique.values())[:10]


def save_message(role, content):
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO messages(student_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (get_student_id(), role, content, utc_now()),
        )
        conn.commit()
    finally:
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


def call_interaction(
    input_text,
    previous_interaction_id=None,
    system_instruction=SYSTEM_PROMPT
):
    # GeminiのInteractions APIからGroqのChat Completions APIへ変更
    # 会話履歴はbuild_context()で取得して入力に含める
    client = get_client()

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": system_instruction
            },
            {
                "role": "user",
                "content": input_text
            }
        ],
        temperature=0.7,
    )

    answer = completion.choices[0].message.content or ""
    answer = answer.strip()

    if not answer:
        raise RuntimeError("Groqからテキスト回答が返りませんでした。")

    # 既存の呼び出し元との互換性を保つためIDと回答を返す
    return completion.id, answer


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
correct は true または false にしてください。
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
    try:
        conn.execute(
            """
            INSERT INTO topic_mastery
            (student_id, subject, topic, level, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, subject, topic)
            DO UPDATE SET
                level=excluded.level,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                get_student_id(),
                subject,
                topic,
                level,
                note,
                utc_now()
            ),
        )
        conn.commit()
    finally:
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


@app.get("/api/suggestions")
def suggestions():
    return jsonify({
        "suggestions": get_learning_suggestions()
    })


@app.post("/api/chat")
def chat():
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get("message", "")).strip()

        if not user_message:
            return jsonify({"error": "メッセージを入力してください。"}), 400

        context = build_context()

        prompt = f"""
{context}

【今回の生徒の発言】
{user_message}

この発言に対して、個別指導教師として回答してください。
生徒が自分で考えられるように、必要なら質問やヒントを使ってください。
"""

        save_message("user", user_message)

        interaction_id, answer = call_interaction(prompt)

        save_message("assistant", answer)

        # 古いDB構造との互換性のためID欄は維持する。
        # Groqでは次の会話をこのIDで再開せず、messagesの履歴を利用する。
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE students
                SET interaction_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (interaction_id, utc_now(), get_student_id())
            )
            conn.commit()
        finally:
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
        try:
            conn.execute(
                """
                INSERT INTO messages(student_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    get_student_id(),
                    "system",
                    f"問題生成: {subject}/{topic}/{difficulty}\n{result['question']}",
                    utc_now(),
                ),
            )
            conn.commit()
        finally:
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
        try:
            conn.execute(
                """
                INSERT INTO attempts
                (student_id, subject, topic, question, answer,
                 correct, feedback, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    get_student_id(),
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
        finally:
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

@app.get("/api/learning-stats")
def api_learning_stats():
    student_id = get_student_id()
    conn = get_db()

    try:
        # 全体の採点回数と正解数
        summary = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(correct), 0) AS correct_count
            FROM attempts
            WHERE student_id = ?
            """,
            (student_id,)
        ).fetchone()

        total = summary["total"]
        correct_count = summary["correct_count"]

        # 教科ごとの正答率
        rows = conn.execute(
            """
            SELECT
                subject,
                COUNT(*) AS total,
                COALESCE(SUM(correct), 0) AS correct_count
            FROM attempts
            WHERE student_id = ?
            GROUP BY subject
            ORDER BY subject
            """,
            (student_id,)
        ).fetchall()

        subject_stats = []

        for row in rows:
            count = row["total"]
            correct = row["correct_count"]

            subject_stats.append({
                "subject": row["subject"],
                "total": count,
                "correct": correct,
                "accuracy": round(correct / count * 100)
                if count > 0 else 0
            })

    finally:
        conn.close()

    # 分野ごとの理解度を取得
    mastery = get_mastery()

    return jsonify({
        "total_attempts": total,
        "correct_attempts": correct_count,
        "accuracy": round(correct_count / total * 100)
        if total > 0 else 0,
        "subject_stats": subject_stats,
        "mastery": mastery
    })


if __name__ == "__main__":
    print(f"使用モデル: {MODEL}")
    print("AI家庭教師サーバーを起動します。")
    app.run(debug=True, host="127.0.0.1", port=5000)

