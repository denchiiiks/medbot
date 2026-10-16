import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
from openai import OpenAI
import sqlite3
import json
import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
import re
import threading
from flask import Flask

VK_GROUP_TOKEN = os.getenv('VK_GROUP_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
if not VK_GROUP_TOKEN or not OPENROUTER_API_KEY:
    print("ОШИБКА: Не заданы переменные окружения VK_GROUP_TOKEN и OPENROUTER_API_KEY!")
    sys.exit(1)

MODEL_NAME = "openai/gpt-4o-mini"
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
        "X-Title": "MedCheckBot"
    }
)

conn = sqlite3.connect('history.db', check_same_thread=False)
cur = conn.cursor()
cur.execute('''CREATE TABLE IF NOT EXISTS messages (
    user_id INTEGER, role TEXT, content TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()

SYSTEM_PROMPT = {
    "role": "system",
    "content": """Ты — помощник по распознаванию псевдолечения и поиску надёжных медицинских советов.
Твоя задача — анализировать утверждения о здоровье и медицине, которые присылает пользователь, и определять, есть ли признаки псевдонауки.

Используй следующие критерии КРАСНЫХ ФЛАГОВ (признаки псевдолечения):
1. «Секретные» или «универсальные» методики — обещание вылечить одним средством сразу много болезней.
2. Отказ от официальной медицины — призывы бросить назначенное врачом лечение.
3. Громкие и бездоказательные заявления — «100% гарантия», «панацея», «мгновенный эффект», «чудо-средство».
4. Теории заговора — «фармкомпании скрывают», «врачи молчат».
5. Опора только на отзывы и личные истории — «мне помогло», «бабушка вылечилась».
6. Агрессивные продажи — подталкивание к покупке БАДа, курса, прибора по «акции».

Признаки НАДЁЖНЫХ И АВТОРИТЕТНЫХ СОВЕТОВ:
- Ссылки на авторитетные источники (ВОЗ, Минздрав, Cochrane, PubMed, рецензируемые журналы).
- Взвешенный и сдержанный тон, упоминание побочных эффектов и ограничений.
- Понятный статус автора (врач, профильный журналист с указанием квалификации).
- Призыв обратиться к специалисту, напоминание, что информация носит ознакомительный характер.

При анализе утверждения:
1. Определи, есть ли красные флаги. Перечисли их.
2. Определи, есть ли признаки надёжности. Перечисли их.
3. Сделай вывод: утверждение выглядит как псевдонаучное, сомнительное или заслуживающее доверия.
4. Если возможно, порекомендуй обратиться к официальным источникам: Cochrane (cochrane.org/ru/evidence), МедПортал (medportal.ru), Купрум (cuprum.media).
5. Всегда напоминай, что ты не врач и не заменяешь консультацию специалиста.

Отвечай на русском языке. Не используй Markdown-разметку. Пиши обычным текстом, для выделения используй переносы строк и заглавные буквы."""
}

def clean_markdown(text):
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = text.replace('*', '')
    return text

def load_context(user_id):
    cur.execute(
        "SELECT role, content FROM messages WHERE user_id=? ORDER BY timestamp ASC LIMIT ?",
        (user_id, CONTEXT_LENGTH)
    )
    return [{"role": r[0], "content": r[1]} for r in cur.fetchall()]

def save_message(user_id, role, content):
    if len(content) > 2000:
        content = content[:2000]
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
        answer = clean_markdown(response.choices[0].message.content)
        save_message(user_id, "user", prompt)
        save_message(user_id, "assistant", answer)
        return answer
    except Exception as e:
        print(f"OpenRouter error: {e}")
        return "Извини, произошла техническая ошибка. Попробуй позже."

def generate_test():
    prompt = (
        "Сгенерируй тест из 5 вопросов на тему медицинских мифов и псевдолечения. "
        "Каждый вопрос с 4 вариантами ответов и укажи правильный. "
        "Верни строго JSON массив объектов с ключами: question, options (список), answer (буква варианта)."
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
        return []
    except Exception as e:
        print(f"Test generation error: {e}")
        return []

def get_main_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('Проверить утверждение', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('Красные флаги', color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button('Надёжные источники', color=VkKeyboardColor.POSITIVE)
    keyboard.add_button('Проверь себя', color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button('Помощь', color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()

def get_sources_text():
    return (
        "Надёжные источники по доказательной медицине:\n\n"
        "1. Cochrane (Кокрейновские обзоры) — https://www.cochrane.org/ru/evidence\n"
        "   Собирает все исследования по теме, отсеивает мусорные и делает строгий вывод.\n\n"
        "2. МедПортал — https://medportal.ru/\n"
        "   Один из старейших медицинских сайтов Рунета. Разбирает мифы и фейки на основе доказательного подхода.\n\n"
        "3. Журнал «Купрум» — https://cuprum.media/\n"
        "   Современное медиа о здоровье. Коротко и понятно разбирает медицинские фейки и тренды из TikTok.\n\n"
        "Помни: я не врач. При серьёзных symptoms обращайся к специалисту."
    )

def get_red_flags_text():
    return (
        "КРАСНЫЕ ФЛАГИ — признаки псевдолечения:\n\n"
        "1. «Секретные» или «универсальные» методики — обещают вылечить одним средством сразу много болезней.\n"
        "2. Отказ от официальной медицины — призывы бросить назначенное врачом лечение.\n"
        "3. Громкие заявления — «100% гарантия», «панацея», «мгновенный эффект», «чудо-средство».\n"
        "4. Теории заговора — «фармкомпании скрывают», «врачи молчат».\n"
        "5. Опора только на отзывы — «мне помогло», «бабушка вылечилась».\n"
        "6. Агрессивные продажи — подталкивание к покупке БАДа, курса, прибора по «акции».\n\n"
        "Если видишь хотя бы один из этих признаков — относись к информации с крайним скептицизмом."
    )

def get_checklist_text():
    return (
        "ЧЕК-ЛИСТ: задайте себе 4 вопроса, столкнувшись с медицинским советом в интернете:\n\n"
        "1. Кто это говорит? Есть ли у автора высшее медицинское образование и действующая практика?\n"
        "2. Чем это подтверждается? Есть ли ссылки на крупные исследования или всё строится на личных рассуждениях?\n"
        "3. Что мне пытаются продать? Это текст с бескорыстным просвещением или скрытая реклама?\n"
        "4. Совпадает ли это с мнением официальной науки? Если метод уникален и о нём пишет только один сайт — скорее всего, это обман."
    )

app = Flask(__name__)

@app.route('/')
def home():
    return "MedBot is alive"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

def run_bot():
    print("Медицинский бот запущен...")
    active_tests = {}

    for event in longpoll.listen():
        if event.type != VkEventType.MESSAGE_NEW or not event.to_me or not event.from_user:
            continue

        user_id = event.user_id
        text = event.text.lower() if event.text else ""

        if text in ("начать", "привет"):
            vk.messages.send(
                user_id=user_id,
                message="Привет! Я помогу распознать псевдолечение и найти надёжные медицинские советы. "
                        "Пришли утверждение, и я проверю его на красные флаги. Или выбери действие на клавиатуре.",
                keyboard=get_main_keyboard(),
                random_id=0
            )
            continue

        if text == "помощь":
            help_msg = (
                "Я умею:\n"
                "- Проверять утверждения о здоровье на признаки псевдонауки (просто напиши утверждение)\n"
                "- Показывать красные флаги псевдолечения (кнопка «Красные флаги»)\n"
                "- Давать ссылки на надёжные источники (кнопка «Надёжные источники»)\n"
                "- Проводить тест на знание медицинских мифов (кнопка «Проверь себя»)\n\n"
                "Фото и документы пока не поддерживаются. Пришли текст."
            )
            vk.messages.send(user_id=user_id, message=help_msg, keyboard=get_main_keyboard(), random_id=0)
            continue

        if text == "красные флаги":
            vk.messages.send(user_id=user_id, message=get_red_flags_text(), keyboard=get_main_keyboard(), random_id=0)
            continue

        if text == "надёжные источники":
            vk.messages.send(user_id=user_id, message=get_sources_text(), keyboard=get_main_keyboard(), random_id=0)
            continue

        if text == "проверить утверждение":
            vk.messages.send(user_id=user_id,
                             message="Напиши утверждение о здоровье, которое хочешь проверить. Например: «Этот БАД вылечит простуду за один день».",
                             random_id=0)
            continue

        if text == "проверь себя":
            questions = generate_test()
            if not questions:
                vk.messages.send(user_id=user_id, message="Не удалось создать тест. Попробуй позже.", keyboard=get_main_keyboard(), random_id=0)
                continue
            active_tests[user_id] = {
                "stage": "asking",
                "questions": questions,
                "current_q": 0,
                "score": 0
            }
            q = questions[0]
            opts = "\n".join(q["options"])
            vk.messages.send(user_id=user_id,
                             message=f"Вопрос 1 из {len(questions)}:\n{q['question']}\n{opts}\n\nОтправь букву ответа (a, б, в, г).",
                             random_id=0)
            continue

        if user_id in active_tests:
            test_data = active_tests[user_id]
            if test_data.get("stage") == "asking":
                user_answer = event.text.lower().strip()
                q_index = test_data["current_q"]
                questions = test_data["questions"]
                correct_letter = questions[q_index]["answer"].lower()
                mapping = {'а': 'a', 'б': 'b', 'в': 'c', 'г': 'd', 'a': 'a', 'b': 'b', 'c': 'c', 'd': 'd'}
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
                    msg = f"{feedback}\n\nВопрос {next_q+1} из {len(questions)}:\n{q['question']}\n{opts}\n\nТвой ответ (буква):"
                    vk.messages.send(user_id=user_id, message=msg, random_id=0)
                else:
                    total = len(questions)
                    score = test_data["score"]
                    vk.messages.send(user_id=user_id,
                                     message=f"{feedback}\n\nТест завершён! Твой результат: {score} из {total}.",
                                     keyboard=get_main_keyboard(),
                                     random_id=0)
                    del active_tests[user_id]
                continue

        if event.attachments:
            vk.messages.send(user_id=user_id,
                             message="Извини, я пока не умею обрабатывать фото. Пришли утверждение текстом.",
                             keyboard=get_main_keyboard(),
                             random_id=0)
            continue

        if text:
            answer = ask_ai("Проанализируй утверждение: " + event.text, user_id)
            vk.messages.send(user_id=user_id, message=answer, keyboard=get_main_keyboard(), random_id=0)
            continue


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    run_bot()
