import os
import sys
import re
import time
import threading
import logging
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import config
from config import ADMIN_PHONE, TARGET_GROUP_NAME, PROFILE_DIR
from database import DatabaseManager, MIN_INCREMENT

# Silencia avisos de tentativa de reconexão do urllib3 e Selenium
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)

# Configuração do sistema de Log (Salva em bot.log e exibe no console)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

ADMIN_NAMES = [n.strip().lower() for n in getattr(config, "ADMIN_NAMES", [])]
# Por segurança, o nome exibido NÃO vale como admin (qualquer um pode trocar o próprio nome).
# Só ligue isto no config.py (ALLOW_ADMIN_BY_NAME = True) se souber o risco.
ALLOW_ADMIN_BY_NAME = getattr(config, "ALLOW_ADMIN_BY_NAME", False)

READ_LAST_N = 40
POLL_INTERVAL = 1.0
PROCESS_BACKLOG = False

# Ctrl+A no Windows/Linux, Cmd+A no Mac
SELECT_ALL_KEY = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL

MAX_VALUE = 100000000  # limite da coluna DECIMAL(10, 2)

STATUS_LABELS = {
    "ACTIVE": "🟢 Ativo",
    "CLOSED": "🔴 Encerrado",
    "CANCELLED": "⚪ Cancelado",
}

FOOTER_INPUT_XPATH = "(//footer//div[@contenteditable='true'])[1]"

SEARCH_INPUT_XPATH = (
    "//input[@role='textbox' and @data-tab='3'] | "
    "//div[@contenteditable='true'][@data-tab='3']"
)

JS_READ_MESSAGES = """
const limit = arguments[0];
const seen = new Set();
const out = [];
document.querySelectorAll('[data-pre-plain-text]').forEach(copy => {
  const holder = copy.closest('[data-id]');
  const pre = copy.getAttribute('data-pre-plain-text') || '';
  const node = copy.querySelector('.selectable-text') || copy.querySelector('[data-testid="selectable-text"]');
  const text = (node ? (node.innerText || node.textContent || '') : '').trim();
  const id = holder ? holder.getAttribute('data-id') : ('pre:' + pre + '|' + text);
  if (seen.has(id)) return;
  seen.add(id);
  const scope = holder || copy.parentElement;
  let fromMe = false;
  const meta = scope ? scope.querySelector('[data-testid="msg-meta"]') : null;
  if (meta) {
    fromMe = Array.from(meta.querySelectorAll('svg title'))
      .some(t => /^(wds-ic-(read|delivered|sent|time|pending)|msg-)/.test((t.textContent || '').trim()));
  }
  if (!fromMe && scope && scope.querySelector('[aria-label="Você:"], [aria-label="You:"]')) fromMe = true;
  out.push({id: id, fromMe: fromMe, pre: pre, text: text});
});
return out.slice(-limit);
"""

JS_INSERT_TEXT = """
const el = arguments[0];
el.focus();
return document.execCommand('insertText', false, arguments[1]);
"""


def digits(value):
    return re.sub(r"\D", "", value or "")


def normalize_phone(value):
    d = digits(value)
    if d.startswith("55") and len(d) == 13 and d[4] == "9":
        d = d[:4] + d[5:]
    return d


def extract_phone(sender):
    """
    Devolve o telefone normalizado só se o remetente aparece como número
    (contato não salvo). Se aparecer um nome, não dá para saber o telefone: devolve None.
    """
    if re.fullmatch(r"[+\d\s\-()]+", sender or ""):
        d = normalize_phone(sender)
        if len(d) >= 10:
            return d
    return None


def xpath_literal(s):
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


def parse_sender(pre_plain_text):
    m = re.match(r"^\[[^\]]*\]\s*(.*?):\s*$", pre_plain_text or "", re.S)
    name = m.group(1).strip() if m else ""
    return name or "Desconhecido"


def is_admin(sender, from_me=False):
    if from_me:
        return True
    admin_phone = normalize_phone(ADMIN_PHONE)
    phone = extract_phone(sender)
    if admin_phone and phone and phone == admin_phone:
        return True
    if ALLOW_ADMIN_BY_NAME:
        return sender.strip().lower() in ADMIN_NAMES
    return False


class WhatsAppGarimpoBot:
    def __init__(self):
        self.target_group = TARGET_GROUP_NAME
        self.admin_phone = ADMIN_PHONE
        self.db = DatabaseManager()
        self.driver_lock = threading.RLock()
        self.driver = self._init_driver()
        self.processed_msg_ids = set()
        self.running = True

    def _init_driver(self):
        chrome_options = Options()
        chrome_options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(options=chrome_options)
        driver.maximize_window()
        return driver

    def start(self):
        logging.info("Abrindo WhatsApp Web...")
        self.driver.get("https://web.whatsapp.com")

        logging.info("Aguardando carregamento...")

        search_xpath = "//div[@id='pane-side'] | //div[@contenteditable='true'] | //*[@data-tab='3']"

        try:
            WebDriverWait(self.driver, 120).until(
                EC.presence_of_element_located((By.XPATH, search_xpath))
            )
            logging.info("WhatsApp Web carregado com sucesso!")
            time.sleep(3)
        except Exception as e:
            logging.error(f"Timeout no carregamento: {e}")
            return

        if not self._select_chat(self.target_group):
            self.stop_bot()
            return

        if not PROCESS_BACKLOG:
            self._prime_backlog()

        logging.info("Bot de Garimpo iniciado e escutando comandos...")
        self._listen_messages()

    def stop_bot(self):
        logging.info("Encerrando bot...")
        self.running = False

        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                try:
                    self.driver.quit()
                except Exception:
                    pass

        logging.info("Bot finalizado com sucesso.")
        os._exit(0)

    def _chat_is_open(self, chat_name):
        try:
            with self.driver_lock:
                xp = (
                    f"//*[@id='main']//header//span"
                    f"[contains(@title, {xpath_literal(chat_name)}) "
                    f"or contains(normalize-space(.), {xpath_literal(chat_name)})]"
                )
                return len(self.driver.find_elements(By.XPATH, xp)) > 0
        except Exception:
            return False

    def _ensure_chat(self):
        """Garante que o grupo alvo está aberto; se alguém trocou de conversa, volta para ele."""
        if self._chat_is_open(self.target_group):
            return True
        logging.warning(f"Grupo '{self.target_group}' não está aberto. Tentando reabrir...")
        return self._select_chat(self.target_group)

    def _select_chat(self, chat_name):
        if self._chat_is_open(chat_name):
            logging.info(f"Grupo '{chat_name}' já está aberto.")
            return True

        try:
            with self.driver_lock:
                box = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, SEARCH_INPUT_XPATH))
                )
                self.driver.execute_script("arguments[0].focus(); arguments[0].click();", box)
                box.send_keys(SELECT_ALL_KEY, "a")
                box.send_keys(Keys.BACKSPACE)
                box.send_keys(chat_name)

            time.sleep(2.5)  # espera os resultados renderizarem

            with self.driver_lock:
                box = self.driver.find_element(By.XPATH, SEARCH_INPUT_XPATH)
                box.send_keys(Keys.ARROW_DOWN)  # vai para o 1º resultado
                time.sleep(0.5)
                box.send_keys(Keys.ENTER)       # abre a conversa

            time.sleep(2)

            if self._chat_is_open(chat_name):
                logging.info(f"Grupo '{chat_name}' aberto com sucesso.")
                return True
        except Exception as e:
            logging.error(f"Falha ao selecionar '{chat_name}': {e!r}")

        logging.error(
            f"Não foi possível abrir o grupo '{chat_name}'. "
            f"Confira TARGET_GROUP_NAME no config.py (acentos, emojis, espaços)."
        )
        return False

    def send_message(self, text):
        try:
            with self.driver_lock:
                if not self._ensure_chat():
                    logging.error("Mensagem não enviada: grupo alvo não está aberto.")
                    return

                input_box = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, FOOTER_INPUT_XPATH))
                )
                self.driver.execute_script("arguments[0].focus();", input_box)
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if line:
                        ok = self.driver.execute_script(JS_INSERT_TEXT, input_box, line)
                        if not ok:
                            input_box.send_keys(line)
                    if i < len(lines) - 1:
                        input_box.send_keys(Keys.SHIFT, Keys.ENTER)

                input_box.send_keys(Keys.ENTER)
                logging.info(f"[📩 enviada] Conteúdo: '{text[:50]}...'")
        except Exception as e:
            logging.error(f"Erro ao enviar mensagem: {e!r}")

    def _read_rows(self):
        with self.driver_lock:
            return self.driver.execute_script(JS_READ_MESSAGES, READ_LAST_N) or []

    def _prime_backlog(self):
        rows = []
        for _ in range(5):
            rows = self._read_rows()
            if rows:
                break
            time.sleep(1)
        for r in rows:
            self.processed_msg_ids.add(r["id"])

    def _listen_messages(self):
        logging.info("Monitorando mensagens...")
        while self.running:
            try:
                # Se alguém clicou em outra conversa, volta para o grupo antes de ler,
                # para não executar comandos de outro chat.
                if not self._ensure_chat():
                    time.sleep(5)
                    continue

                rows = self._read_rows()

                for r in rows:
                    msg_id, text = r["id"], r["text"]
                    if not text or msg_id in self.processed_msg_ids:
                        continue

                    self.processed_msg_ids.add(msg_id)
                    sender = parse_sender(r["pre"])
                    origem = "enviada" if r["fromMe"] else "recebida"

                    logging.info(f"[📩 {origem}] De: {sender} | Conteúdo: '{text}'")

                    if not text.startswith("!"):
                        continue

                    self._process_command(text, sender, r["fromMe"])

            except Exception as e:
                logging.error(f"Erro na leitura de mensagens: {e!r}")
                time.sleep(2)

            time.sleep(POLL_INTERVAL)

    def _process_command(self, text, sender, from_me=False):
        args = text.split()
        if not args:
            return

        cmd = args[0].lower()
        admin = is_admin(sender, from_me)

        # 1. CRIAR GARIMPO (Sintaxe: !garimpo Nome do Item, Preço Inicial)
        if cmd == "!garimpo":
            if not admin:
                self.send_message("❌ Apenas o administrador pode criar garimpos.")
                return

            try:
                payload = text[len("!garimpo"):].strip()

                # Separa nome e preço na vírgula. O preço pode ter vírgula decimal (10,50),
                # então o regex pega o nome e o número final.
                m = re.match(r"^(.*?),\s*(\d+(?:[.,]\d{1,2})?)\s*$", payload, re.S)
                if not m:
                    raise ValueError("Formato incorreto.")

                item_name = m.group(1).strip()
                start_price = float(m.group(2).replace(",", "."))

                if not item_name:
                    raise ValueError("Nome vazio.")
                if start_price < 0 or start_price >= MAX_VALUE:
                    raise ValueError("Preço fora do limite.")

                code = self.db.create_garimpo(item_name, start_price)

                msg = (
                    f"⛏️ *GARIMPO REGISTRADO!* ⛏️\n\n"
                    f"🏷️ *Código:* {code}\n"
                    f"📦 *Item:* {item_name}\n"
                    f"💰 *Preço Inicial:* R$ {start_price:.2f}\n"
                    f"📈 *Incremento mínimo:* R$ {float(MIN_INCREMENT):.2f}\n\n"
                    f"👉 Para dar um lance, envie: `!lance {code} <valor>`"
                )
                self.send_message(msg)
            except Exception as e:
                logging.error(f"Falha em !garimpo: {e!r}")
                self.send_message("⚠️ Sintaxe incorreta. Use: `!garimpo [Nome do Item], [Preço Inicial]`")

        # 2. ENCERRAR GARIMPO MANUALMENTE
        elif cmd in ["!encerrar_garimpo", "!fechar_garimpo", "!encerrar"]:
            if not admin:
                self.send_message("❌ Apenas o administrador pode encerrar um garimpo.")
                return

            try:
                code = args[1].upper()
                garimpo_alvo = self.db.get_garimpo_by_code(code)

                if not garimpo_alvo or not self.db.force_close_garimpo(code):
                    self.send_message(f"⚠️ Nenhum garimpo ativo encontrado com o código *{code}*.")
                    return

                if garimpo_alvo['current_winner']:
                    msg = (
                        f"🎉 *GARIMPO ENCERRADO!* 🎉\n\n"
                        f"🏷️ *Código:* {garimpo_alvo['code']}\n"
                        f"📦 *Item:* {garimpo_alvo['card_name']}\n"
                        f"🏆 *Vencedor:* {garimpo_alvo['current_winner']}\n"
                        f"💵 *Valor Final:* R$ {garimpo_alvo['current_bid']:.2f}"
                    )
                else:
                    msg = f"🔔 *GARIMPO ENCERRADO SEM LANCES* 🔔\n\nO garimpo *{garimpo_alvo['code']}* foi encerrado sem lances."

                self.send_message(msg)
            except IndexError:
                self.send_message("⚠️ Informe o código do garimpo. Exemplo: `!encerrar_garimpo #PKM-01`")

        # 3. ENCERRAR TODOS OS GARIMPOS
        elif cmd in ["!encerrar_todos", "!fechar_todos", "!encerrar_tudo"]:
            if not admin:
                self.send_message("❌ Apenas o administrador pode encerrar todos os garimpos.")
                return

            garimpos = self.db.close_all_garimpos()
            if not garimpos:
                self.send_message("📭 Nenhum garimpo ativo para encerrar.")
                return

            msg = f"🔒 *TODOS OS GARIMPOS ENCERRADOS ({len(garimpos)})* 🔒\n\n"
            for g in garimpos:
                if g['current_winner']:
                    msg += (
                        f"• *{g['code']}* - {g['card_name']} | "
                        f"🏆 {g['current_winner']} | R$ {g['current_bid']:.2f}\n"
                    )
                else:
                    msg += f"• *{g['code']}* - {g['card_name']} | Sem lances\n"

            self.send_message(msg)

        # 4. CANCELAR GARIMPO (criado por engano; não entra no relatório de vendas)
        elif cmd in ["!cancelar_garimpo", "!cancelar"]:
            if not admin:
                self.send_message("❌ Apenas o administrador pode cancelar um garimpo.")
                return

            try:
                code = args[1].upper()
                garimpo_alvo = self.db.get_garimpo_by_code(code)

                if not garimpo_alvo or not self.db.cancel_garimpo(code):
                    self.send_message(f"⚠️ Nenhum garimpo ativo encontrado com o código *{code}*.")
                    return

                msg = (
                    f"⚪ *GARIMPO CANCELADO* ⚪\n\n"
                    f"🏷️ *Código:* {garimpo_alvo['code']}\n"
                    f"📦 *Item:* {garimpo_alvo['card_name']}"
                )
                if garimpo_alvo['current_winner']:
                    msg += "\n\nOs lances deste garimpo foram descartados."
                self.send_message(msg)
            except IndexError:
                self.send_message("⚠️ Informe o código do garimpo. Exemplo: `!cancelar_garimpo #PKM-01`")

        # 5. DAR LANCE
        elif cmd == "!lance":
            try:
                code, bid_value = args[1].upper(), float(args[2].replace(",", "."))
                success, result = self.db.place_bid(code, bid_value, sender, extract_phone(sender))

                if success:
                    msg = f"✅ *LANCE ACEITO!*\n\nO usuário *{sender}* deu um lance de *R$ {bid_value:.2f}* para o garimpo *{code}*."
                    self.send_message(msg)
                else:
                    self.send_message(f"❌ {result}")
            except (IndexError, ValueError):
                self.send_message("⚠️ Formato incorreto. Use: `!lance #PKM-01 150`")

        # 6. EXIBIR GARIMPOS ATIVOS
        elif cmd in ["!catalogo", "!garimpos"]:
            garimpos = self.db.get_active_garimpos()
            if not garimpos:
                self.send_message("📭 Nenhum garimpo ativo no momento.")
                return

            msg = "📋 *GARIMPOS ATIVOS NO MOMENTO* 📋\n\n"
            for a in garimpos:
                msg += (
                    f"🔹 *Código:* {a['code']}\n"
                    f"   Item: {a['card_name']}\n"
                    f"   Lance Atual: R$ {a['current_bid']:.2f} ({a['current_winner'] or 'Sem lances'})\n"
                    f"-----------------------------\n"
                )
            self.send_message(msg)

        # 7. GARIMPOS RECENTES (ativos + encerrados + cancelados). Uso: !recentes ou !recentes 20
        elif cmd in ["!recentes", "!ultimos"]:
            limit = 10
            if len(args) > 1 and args[1].isdigit():
                limit = max(1, min(int(args[1]), 20))

            recentes = self.db.get_recent_garimpos(limit)
            if not recentes:
                self.send_message("📭 Nenhum garimpo registrado ainda.")
                return

            msg = f"🕒 *GARIMPOS RECENTES ({len(recentes)})* 🕒\n\n"
            for g in recentes:
                status = STATUS_LABELS.get(g['status'], g['status'])
                vencedor = g['current_winner'] or "Sem lances"
                msg += (
                    f"🔹 *{g['code']}* - {g['card_name']}\n"
                    f"   {status} | R$ {g['current_bid']:.2f} | {vencedor}\n"
                    f"-----------------------------\n"
                )
            self.send_message(msg)

        # 8. MEUS LANCES (garimpos em que a pessoa está ganhando ou ganhou)
        elif cmd in ["!meuslances", "!meus_lances"]:
            meus = self.db.get_my_garimpos(sender, extract_phone(sender))
            if not meus:
                self.send_message(f"📭 *{sender}*, você não tem nenhum lance vencedor no momento.")
                return

            msg = f"🙋 *LANCES DE {sender.upper()}* 🙋\n\n"
            for g in meus:
                situacao = "🏆 Ganhando" if g['status'] == 'ACTIVE' else "✅ Ganhou"
                msg += (
                    f"🔹 *{g['code']}* - {g['card_name']}\n"
                    f"   {situacao} | R$ {g['current_bid']:.2f}\n"
                    f"-----------------------------\n"
                )
            self.send_message(msg)

        # 9. RELATÓRIO DE VENDAS
        elif cmd == "!relatorio":
            if not admin:
                return

            report = self.db.get_sales_report()
            if not report:
                self.send_message("📊 Nenhum garimpo finalizado no histórico.")
                return

            vendidos = [i for i in report if i['current_winner']]
            total = sum(float(i['current_bid']) for i in vendidos)
            mostrados = report[:50]

            msg = "📊 *RELATÓRIO DE VENDAS ENCERRADAS* 📊\n\n"
            for item in mostrados:
                vencedor = item['current_winner'] or "Sem lances"
                msg += f"• *{item['code']}* - {item['card_name']} | Arremate: R$ {item['current_bid']:.2f} | Comprador: {vencedor}\n"

            msg += (
                f"\n💰 *Total arrecadado:* R$ {total:.2f} "
                f"({len(vendidos)} vendido(s), {len(report) - len(vendidos)} sem lances)"
            )
            if len(report) > len(mostrados):
                msg += f"\n(Mostrando os {len(mostrados)} mais recentes de {len(report)})"
            self.send_message(msg)


if __name__ == "__main__":
    bot = WhatsAppGarimpoBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop_bot()