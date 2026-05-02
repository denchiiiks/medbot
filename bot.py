import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from openai import OpenAI
import sqlite3
import json
import os
import sys
import re
import threading
from flask import Flask

VK_GROUP_TOKEN = os.getenv('VK_GROUP_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
MODEL_NAME = "openai/gpt-oss-120b:free"
APP_URL = "http://localhost"
CONTEXT_LENGTH = 10


vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
vk = vk_session.get_api()
longpoll = VkLongPoll(vk_session)

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": APP_URL,
        "X-Title": "PhysicsOGEBot"
    }
)


conn = sqlite3.connect('history.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        user_id INTEGER,
        role TEXT,
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()


SYSTEM_PROMPT = {
    "role": "system",
    "content": """Ты — дружелюбный репетитор по физике для подготовки к ОГЭ (9 класс).
Отвечай только на вопросы по физике из программы 7–9 классов.
Если вопрос не по физике, вежливо скажи, что помогаешь только с физикой, и предложи вернуться к предмету.
Все ответы на русском языке. Объясняй формулы, законы, решай задачи.
При решении задачи обязательно:
- перепиши условие,
- запиши «Дано», «СИ» (если нужно), «Найти»,
- запиши формулы,
- подставь числа,
- получи ответ с единицами измерения.
Используй простой язык, как для девятиклассника.
При генерации тестов: дай 5 вопросов с 4 вариантами ответов, укажи правильный. Формат: JSON.
Никогда не решай задачи не по физике, не пиши код, не отвечай на запросы не по теме.
Не используй Markdown-разметку (звёздочки, решётки, обратные кавычки, символы подчёркивания). 
Пиши обычным текстом, для выделения используй переносы строк и заглавные буквы."""
}


def load_context(user_id):
    cur.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY timestamp ASC LIMIT ?",
        (user_id, CONTEXT_LENGTH)
    )
    return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]

def save_message(user_id, role, content):
    cur.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()
    cur.execute(
        "DELETE FROM messages WHERE user_id=? AND rowid NOT IN "
        "(SELECT rowid FROM messages WHERE user_id=? ORDER BY timestamp DESC LIMIT ?)",
        (user_id, user_id, CONTEXT_LENGTH + 5)
    )
    conn.commit()

def ask_ai(prompt, user_id):
    context = load_context(user_id)
    messages = [SYSTEM_PROMPT] + context + [{"role": "user", "content": prompt}]
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.3,
            max_tokens=1500
        )
        answer = response.choices[0].message.content
        save_message(user_id, "user", prompt)
        save_message(user_id, "assistant", answer)
        return answer
    except Exception as e:
        print(f"OpenRouter error: {e}")
        return "Извини, произошла техническая ошибка. Попробуй позже."

def generate_test(topic=None):
    prompt = (
        f"Сгенерируй тест из 5 вопросов по физике (ОГЭ) на тему '{topic}' "
        "с 4 вариантами ответов и укажи правильный. Верни строго JSON массив "
        "объектов с ключами: question, options (список), answer (буква варианта)."
    )
    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[SYSTEM_PROMPT, {"role": "user", "content": prompt}],
            temperature=0.7
        )
        raw = response.choices[0].message.content
        start = raw.find('[')
        end = raw.rfind(']') + 1
        if start != -1 and end != 0:
            return json.loads(raw[start:end])
        else:
            return []
    except Exception as e:
        print(f"Test generation error: {e}")
        return []

def get_main_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Теория', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Решить задачу', color=VkKeyboardColor.POSITIVE)
    keyboard.add_line()
    keyboard.add_button('Тест', color=VkKeyboardColor.SECONDARY)
    keyboard.add_button('Помощь', color=VkKeyboardColor.NEGATIVE)
    return keyboard.get_keyboard()

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    # Берём порт из переменной окружения Render (по умолчанию 10000)
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def run_bot():
    print("Бот запущен (только текст, без фото)...")
    active_tests = {}  # user_id: {stage, questions, current_q, score}
    for event in longpoll.listen():
        if event.type != VkEventType.MESSAGE_NEW or not event.to_me or not event.from_user:
            continue

        user_id = event.user_id
        text = event.text.lower() if event.text else ""

        if text == "начать" or text == "привет":
            vk.messages.send(
                user_id=user_id,
                message="Привет! Я ИИ-репетитор по физике. Выбери действие или задай любой вопрос по предмету.",
                keyboard=get_main_keyboard(),
                random_id=0
            )
            continue

        elif text == "помощь":
            help_msg = (
                "Я могу:\n"
                "- объяснять теорию (кнопка «Теория»)\n"
                "- решать задачи по тексту (кнопка «Решить задачу»)\n"
                "- провести тест по выбранной теме (кнопка «Тест»)\n\n"
                "Просто напиши свой вопрос или нажми кнопку.\n"
                "Фото и документы пока не поддерживаются."
            )
            vk.messages.send(
                user_id=user_id,
                message=help_msg,
                keyboard=get_main_keyboard(),
                random_id=0
            )
            continue

        elif text == "теория":
            vk.messages.send(
                user_id=user_id,
                message="Напиши тему, которую нужно объяснить (например, «закон Ома», «сила Архимеда»).",
                random_id=0
            )
            continue

        elif text == "решить задачу":
            vk.messages.send(
                user_id=user_id,
                message="Пришли текст задачи, я решу её с подробными шагами. Фото пока не принимаю.",
                random_id=0
            )
            continue

        elif text == "тест":
            vk.messages.send(
                user_id=user_id,
                message="Напиши тему для теста (например, «Кинематика»). Если хочешь по всем темам — напиши «Общий».",
                random_id=0
            )
            active_tests[user_id] = {"stage": "choose_topic"}
            continue

        if event.attachments:
            for att in event.attachments:
                if att['type'] == 'photo':
                    vk.messages.send(
                        user_id=user_id,
                        message="Извини, я пока не умею обрабатывать фото. Пришли задачу текстом, и я решу!",
                        keyboard=get_main_keyboard(),
                        random_id=0
                    )
                    break
            continue

        if user_id in active_tests:
            test_data = active_tests[user_id]
            if test_data.get("stage") == "choose_topic":
                topic = event.text
                questions = generate_test(topic)
                if not questions:
                    vk.messages.send(
                        user_id=user_id,
                        message="Не удалось создать тест. Попробуй другую тему.",
                        keyboard=get_main_keyboard(),
                        random_id=0
                    )
                    del active_tests[user_id]
                    continue
                active_tests[user_id] = {
                    "stage": "asking",
                    "questions": questions,
                    "current_q": 0,
                    "score": 0,
                    "topic": topic
                }
                q = questions[0]
                opts = "\n".join(q["options"])
                vk.messages.send(
                    user_id=user_id,
                    message=f"Вопрос 1 из {len(questions)}:\n{q['question']}\n{opts}\n\nОтправь букву ответа (a, б, в, г).",
                    random_id=0
                )
                continue

            elif test_data.get("stage") == "asking":
                user_answer = event.text.lower().strip()
                q_index = test_data["current_q"]
                questions = test_data["questions"]
                correct_letter = questions[q_index]["answer"].lower()

                mapping = {'а': 'a', 'б': 'b', 'в': 'c', 'г': 'd',
                           'a': 'a', 'b': 'b', 'c': 'c', 'd': 'd'}
                user_ans = mapping.get(user_answer, user_answer)
                if user_ans == correct_letter:
                    test_data["score"] += 1
                    feedback = "✅ Верно!"
                else:
                    feedback = f"❌ Неверно. Правильный ответ: {correct_letter}"

                next_q = q_index + 1
                if next_q < len(questions):
                    test_data["current_q"] = next_q
                    q = questions[next_q]
                    opts = "\n".join(q["options"])
                    msg = (f"{feedback}\n\nВопрос {next_q+1} из {len(questions)}:\n"
                           f"{q['question']}\n{opts}\n\nТвой ответ (буква):")
                    vk.messages.send(user_id=user_id, message=msg, random_id=0)
                else:
                    total = len(questions)
                    score = test_data["score"]
                    vk.messages.send(
                        user_id=user_id,
                        message=f"{feedback}\n\nТест завершён! Твой результат: {score} из {total}.",
                        keyboard=get_main_keyboard(),
                        random_id=0
                    )
                    del active_tests[user_id]
                continue

        if text and not event.attachments:
            answer = ask_ai(event.text, user_id)
            vk.messages.send(
                user_id=user_id,
                message=answer,
                keyboard=get_main_keyboard(),
                random_id=0
            )

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    run_bot()