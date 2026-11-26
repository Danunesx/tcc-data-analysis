"""
script.py

Coleta publicações públicas do perfil @cazetv no Instagram dentro de uma
janela temporal (TARGET_START a TARGET_END) e grava os dados em um CSV.

MODOS DE BUSCA (escolhidos na linha de comando com --modo)
  manual (padrão)
      Você rola o feed até o primeiro post a coletar (o mais recente dentro
      da janela). Quando a rolagem ficar parada por MANUAL_IDLE_SECONDS
      segundos, a coleta começa.
  auto
      O script localiza sozinho o ponto da janela: salto exponencial
      adaptativo (estimando posts/hora) + bisseção, lendo a data de alguns
      posts. Ao achar o primeiro post com data <= TARGET_END, a coleta
      começa a partir dele, sem nenhuma intervenção.

  Depois do posicionamento, os dois modos usam a MESMA coleta: o script abre
  cada post em uma aba nova, extrai os dados, fecha a aba e rola o feed.
  Termina ao encontrar posts anteriores a TARGET_START (o feed vai do mais
  recente para o mais antigo).

COMO USAR
    pip install selenium
    python script.py                 # modo manual (padrão)
    python script.py --modo manual
    python script.py --modo auto

  1. LOGIN: o script abre o Instagram e você faz o login manualmente (ele
     não tenta burlar 2FA nem CAPTCHA). Com perfil dedicado do Chrome, o
     login fica salvo para as próximas execuções.
  2. RETOMADA: o CSV e o checkpoint são salvos a cada rodada. Se o Chrome
     travar ou der erro, o script salva e encerra; basta rodar de novo para
     continuar de onde parou. Ctrl+C também salva.

Antes de rodar, ajuste a seção CONFIGURAÇÃO (principalmente a janela de
datas e o caminho do perfil do Chrome) e feche o Chrome normal.

AVISOS
  - O Instagram muda o HTML com frequência e limita requisições. Se algum
    campo vier vazio em todos os posts, os seletores podem precisar de ajuste.
  - Respeite os Termos de Uso do Instagram e as normas éticas da sua
    instituição (dados públicos, sem dados pessoais de terceiros).

CSV GERADO (cazetv_posts.csv)
    post_id, url, post_type, media_count, likes, comments, caption,
    date, time, datetime_iso, collected_at
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Optional

import urllib3.exceptions
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ==========================================================================
# CONFIGURAÇÃO
# ==========================================================================

PROFILE_USERNAME = "cazetv"
PROFILE_URL = f"https://www.instagram.com/{PROFILE_USERNAME}/"

# Fuso da pesquisa. O Instagram informa as datas em UTC; convertemos para o
# horário de Brasília (UTC-3, sem horário de verão desde 2019) para decidir
# a que dia cada post pertence.
TIMEZONE = timezone(timedelta(hours=-3))

# Janela de coleta (inclusive). TARGET_START é o mais antigo, TARGET_END o mais recente.
TARGET_START = datetime(2026, 11, 17, 10, 59, 59, tzinfo=TIMEZONE)
TARGET_END = datetime(2026, 11, 17, 12, 59, 59, tzinfo=TIMEZONE)

# Arquivos de saída
OUTPUT_CSV = "cazetv_posts.csv"
CHECKPOINT_FILE = "checkpoint.json"
LOG_FILE = "scraper.log"

# Perfil dedicado do Chrome (mantém o login entre execuções).
# Para não usar perfil dedicado, coloque None nas duas variáveis.
# Descubra os caminhos em chrome://version:
#   CHROME_USER_DATA_DIR = pasta "User Data" (tudo ANTES de "Profile 2")
#   CHROME_PROFILE_DIRECTORY = só o nome da pasta, ex.: "Profile 2"
CHROME_USER_DATA_DIR: Optional[str] = r"C:\Users\danie\ChromeSeleniumProfile"
CHROME_PROFILE_DIRECTORY: Optional[str] = "Profile 2"

# Tempos (segundos)
ELEMENT_TIMEOUT = 15            # espera por um elemento aparecer na página
PAGE_LOAD_TIMEOUT = 45          # espera máxima pelo carregamento de uma página
SCRIPT_TIMEOUT = 20             # espera máxima por um script JS (evita travar por 120s)
POLITE_DELAY = (1.5, 3.0)       # pausa "humana" entre ações
SCROLL_DELAY = (1.8, 3.2)       # pausa após cada rolagem
MANUAL_IDLE_SECONDS = 30        # rolagem parada por este tempo = começar a coletar
MANUAL_POLL_INTERVAL = 1.0      # de quantos em quantos segundos checar a rolagem

# Robustez
POST_RETRY_ATTEMPTS = 3         # tentativas seguidas para ler um post
POST_RETRY_BACKOFF = 4          # espera entre tentativas (multiplicada pelo nº da tentativa)
ABANDON_AFTER_CYCLES = 3        # após X ciclos de falha, o post é abandonado e registrado
RECOVERY_EVERY_ROUNDS = 5       # rodadas sem avanço antes de tentar recuperar o feed
RECOVERY_SCROLLS = 8            # rolagens extras durante a recuperação
MAX_RECOVERIES = 6              # recuperações seguidas sem avanço antes de encerrar
END_CONFIRMATION_COUNT = 3      # posts seguidos anteriores à janela para encerrar
#   (um único post antigo pode ser um post fixado no topo do perfil)

# Seletor CSS único para links de publicações (posts, reels e vídeos IGTV).
# Usado tanto na listagem de URLs quanto no posicionamento manual.
POST_LINK_SELECTOR = "a[href*='/p/'], a[href*='/reel/'], a[href*='/tv/']"

# Modos de busca (valores aceitos em --modo)
MODE_MANUAL = "manual"
MODE_AUTO = "auto"
DEFAULT_MODE = MODE_MANUAL

# Busca automática (só usada com --modo auto)
INITIAL_RATE_POSTS_PER_HOUR = 10.0   # chute inicial; recalibrado a cada amostra real
SEARCH_MAX_SCROLLS = 400             # rolagens máximas por pedido de "carregar N posts"
SEARCH_STAGNANT_LIMIT = 12           # rolagens seguidas sem posts novos antes de desistir
SEARCH_MAX_GALLOP_ROUNDS = 60        # rodadas máximas do salto exponencial
SEARCH_MIN_JUMP = 25                 # menor salto (em posts) quando longe do alvo
SEARCH_FINE_DAYS = 7                 # a menos de X dias do alvo, saltos menores

# ==========================================================================
# LOG
# ==========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("cazetv_scraper")


class BrowserUnresponsiveError(RuntimeError):
    """Levantada quando o Chrome/ChromeDriver para de responder ou o feed
    trava de vez. O programa principal salva o progresso e encerra."""


# ==========================================================================
# MODELO DE DADOS
# ==========================================================================

@dataclass
class PostRecord:
    """Uma publicação coletada. Cada atributo vira uma coluna do CSV."""

    post_id: str                    # código curto da URL (ex.: "C7xYz12AbC")
    url: str
    post_type: str                  # "image" | "video" | "carousel"
    media_count: Optional[int]      # None em carrosséis (não dá para contar com confiança)
    likes: Optional[int]            # None se o Instagram ocultar as curtidas
    comments: Optional[int]
    caption: str
    date: str                       # AAAA-MM-DD no fuso da pesquisa
    time: str                       # HH:MM:SS no fuso da pesquisa
    datetime_iso: str               # ISO 8601 com fuso (ex.: 2026-05-03T14:22:11-03:00)
    collected_at: str               # quando o dado foi coletado (curtidas mudam com o tempo)


CSV_FIELDS = [f.name for f in fields(PostRecord)]


# ==========================================================================
# FUNÇÕES AUXILIARES (sem Selenium)
# ==========================================================================

def polite_sleep(min_s: float, max_s: float) -> None:
    """Dorme um tempo aleatório entre `min_s` e `max_s` segundos.

    Imita o ritmo de uma pessoa e evita sobrecarregar o servidor.
    """
    time.sleep(random.uniform(min_s, max_s))


_SUFFIXES = {
    "k": 1_000, "mil": 1_000,
    "m": 1_000_000, "mi": 1_000_000,
    "milhão": 1_000_000, "milhao": 1_000_000,
    "milhões": 1_000_000, "milhoes": 1_000_000,
}
_SUFFIX_PATTERN = "milhões|milhoes|milhão|milhao|mil|mi|k|m"
# O lookahead (?![a-zà-ú]) impede confundir o sufixo com o início de outra palavra ("min", "mês").
_COUNT_RE = re.compile(
    rf"(?P<num>\d[\d.,]*)\s*(?P<suffix>{_SUFFIX_PATTERN})?(?![a-zà-ú])", re.IGNORECASE
)
_LIKES_RE = re.compile(
    rf"([\d.,]+\s*(?:{_SUFFIX_PATTERN})?)\s+(?:likes?|curtidas?)", re.IGNORECASE
)
_COMMENTS_RE = re.compile(
    rf"([\d.,]+\s*(?:{_SUFFIX_PATTERN})?)\s+(?:comments?|coment[áa]rios?)", re.IGNORECASE
)
_CAPTION_RE = re.compile(r'^.*? - .*?:\s*[“"](?P<caption>.*)[”"]\.?\s*$', re.DOTALL)
_SHORTCODE_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)")


def parse_count_text(text: Optional[str]) -> Optional[int]:
    """Converte um texto de contagem em inteiro.

    Exemplos: '12,3 mil' -> 12300 | '1.2M' -> 1200000 | '1.234' -> 1234 |
    '2,5 milhões' -> 2500000.
    Com sufixo (mil, mi, k, m) o separador é decimal; sem sufixo, '.' e ','
    são separadores de milhar. Retorna None se não houver número válido.
    """
    if not text:
        return None
    match = _COUNT_RE.search(text.strip())
    if not match:
        return None

    raw = match.group("num")
    suffix = (match.group("suffix") or "").lower()
    if suffix:
        number = raw.replace(",", ".")
        if number.count(".") > 1:
            return None  # ambíguo: melhor vazio do que um valor errado
        multiplier = _SUFFIXES[suffix]
    else:
        number = re.sub(r"[.,]", "", raw)
        multiplier = 1

    try:
        return int(round(float(number) * multiplier))
    except ValueError:
        return None


def parse_og_description(description: Optional[str]) -> tuple[Optional[int], Optional[int], str]:
    """Extrai (curtidas, comentários, legenda) da meta tag `og:description`.

    Formato típico (PT ou EN): '1.234 curtidas, 56 comentários - perfil em
    3 de maio de 2026: "legenda"'. Campos não encontrados voltam como None
    (números) ou "" (legenda).
    """
    if not description:
        return None, None, ""

    header = description.split(" - ", 1)[0]  # evita achar "likes" dentro da legenda
    likes_match = _LIKES_RE.search(header)
    comments_match = _COMMENTS_RE.search(header)
    caption_match = _CAPTION_RE.match(description)

    likes = parse_count_text(likes_match.group(1)) if likes_match else None
    comments = parse_count_text(comments_match.group(1)) if comments_match else None
    caption = caption_match.group("caption").strip() if caption_match else ""
    return likes, comments, caption


def shortcode_from_url(url: str) -> Optional[str]:
    """Devolve o código curto de uma URL de post (/p/, /reel/ ou /tv/), ou None."""
    match = _SHORTCODE_RE.search(url)
    return match.group(1) if match else None


def normalize_post_url(href: Optional[str]) -> Optional[str]:
    """Limpa uma URL de post (remove ?query, #fragmento e '/' final).

    Retorna None se `href` for vazio ou não for link de publicação.
    """
    if not href:
        return None
    clean = href.split("?")[0].split("#")[0].rstrip("/")
    return clean if shortcode_from_url(clean) else None


def parse_instagram_datetime(iso_value: str) -> datetime:
    """Converte o atributo `datetime` da tag <time> (UTC, ex.:
    '2026-05-03T14:22:11.000Z') para um datetime no fuso da pesquisa."""
    parsed = datetime.fromisoformat(iso_value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(TIMEZONE)


# ==========================================================================
# PERSISTÊNCIA (CSV E CHECKPOINT)
# ==========================================================================

def _atomic_write(path: str, write_fn, **open_kwargs) -> None:
    """Grava um arquivo de forma segura: escreve em um arquivo temporário e
    só então substitui o original. Se o computador travar no meio da
    gravação, o arquivo anterior continua íntegro."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", **open_kwargs) as f:
        write_fn(f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def save_results_csv(results: dict[str, PostRecord]) -> None:
    """Salva todos os posts coletados em OUTPUT_CSV, do mais recente para o
    mais antigo (UTF-8 com BOM, para abrir corretamente no Excel)."""
    if not results:
        return
    rows = sorted(results.values(), key=lambda r: r.datetime_iso, reverse=True)

    def write(f) -> None:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in rows:
            writer.writerow(asdict(record))

    _atomic_write(OUTPUT_CSV, write, encoding="utf-8-sig", newline="")


def _to_optional_int(value: Optional[str]) -> Optional[int]:
    """Converte texto do CSV em int; vazio ou inválido vira None."""
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def load_results_csv() -> dict[str, PostRecord]:
    """Recarrega os posts já salvos em OUTPUT_CSV (para retomar sem perder o
    que foi coletado antes). Linhas inválidas são ignoradas.
    Retorna um dicionário {post_id: PostRecord}; vazio se o arquivo não existir."""
    results: dict[str, PostRecord] = {}
    if not os.path.exists(OUTPUT_CSV):
        return results

    try:
        with open(OUTPUT_CSV, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    record = PostRecord(
                        post_id=row["post_id"],
                        url=row["url"],
                        post_type=row.get("post_type") or "",
                        media_count=_to_optional_int(row.get("media_count")),
                        likes=_to_optional_int(row.get("likes")),
                        comments=_to_optional_int(row.get("comments")),
                        caption=row.get("caption") or "",
                        date=row.get("date") or "",
                        time=row.get("time") or "",
                        datetime_iso=row.get("datetime_iso") or "",
                        collected_at=row.get("collected_at") or "",
                    )
                except KeyError:
                    continue
                results[record.post_id] = record
    except OSError as e:
        log.warning(f"Não foi possível ler {OUTPUT_CSV}: {e}")

    if results:
        log.info(f"{len(results)} publicações recarregadas de {OUTPUT_CSV}.")
    return results


def save_checkpoint(processed_urls: set[str], abandoned_urls: set[str]) -> None:
    """Salva em CHECKPOINT_FILE as URLs já processadas (para não reabri-las
    na retomada) e as URLs abandonadas após falhas repetidas (para auditoria)."""
    data = {
        "processed_urls": sorted(processed_urls),
        "abandoned_urls": sorted(abandoned_urls),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write(
        CHECKPOINT_FILE,
        lambda f: json.dump(data, f, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_checkpoint() -> tuple[set[str], set[str]]:
    """Lê CHECKPOINT_FILE. Retorna (urls_processadas, urls_abandonadas);
    dois conjuntos vazios se não existir checkpoint ou se ele estiver corrompido."""
    if not os.path.exists(CHECKPOINT_FILE):
        return set(), set()
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        processed = set(data.get("processed_urls", []))
        abandoned = set(data.get("abandoned_urls", []))
    except (OSError, ValueError) as e:
        log.warning(f"Checkpoint ilegível ({e}); começando sem ele.")
        return set(), set()

    log.info(f"Checkpoint carregado: {len(processed)} URLs processadas, "
             f"{len(abandoned)} abandonadas.")
    return processed, abandoned


def save_progress(
    results: dict[str, PostRecord], processed_urls: set[str], abandoned_urls: set[str]
) -> None:
    """Grava de uma vez o CSV e o checkpoint (o "salvar tudo" do programa)."""
    save_results_csv(results)
    save_checkpoint(processed_urls, abandoned_urls)
    log.info(f"[Salvo] {len(results)} publicações | {len(processed_urls)} URLs processadas.")


# ==========================================================================
# NAVEGADOR
# ==========================================================================

# Falhas de comunicação com o Chrome. O Selenium nem sempre converte todas em
# WebDriverException: um timeout de leitura pode vazar como erro cru do urllib3.
BROWSER_ERRORS = (TimeoutException, WebDriverException, urllib3.exceptions.HTTPError)


def build_driver() -> webdriver.Chrome:
    """Abre o Chrome controlado pelo Selenium.

    Se CHROME_USER_DATA_DIR estiver definido, usa esse perfil (cookies e
    login salvos). Feche todas as janelas do Chrome normal antes: o Chrome não
    permite dois processos no mesmo perfil. Também define os timeouts e
    reduz os sinais de automação do navegador.
    """
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,1600")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if CHROME_USER_DATA_DIR:
        options.add_argument(f"--user-data-dir={CHROME_USER_DATA_DIR}")
    if CHROME_PROFILE_DIRECTORY:
        options.add_argument(f"--profile-directory={CHROME_PROFILE_DIRECTORY}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    driver.set_script_timeout(SCRIPT_TIMEOUT)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def safe_quit_driver(driver: Optional[webdriver.Chrome]) -> None:
    """Fecha o Chrome sem deixar um navegador travado derrubar o programa."""
    if driver is None:
        return
    try:
        driver.quit()
    except Exception as e:
        log.warning(f"driver.quit() falhou: {e}")
        try:
            driver.service.stop()
        except Exception:
            pass


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """Informa se a sessão do Instagram está autenticada, procurando o cookie
    'sessionid' (só existe para sessões logadas; mais confiável que procurar
    elementos na tela, que mudam com idioma e layout)."""
    try:
        return any(c.get("name") == "sessionid" and c.get("value") for c in driver.get_cookies())
    except WebDriverException:
        return False


def login(driver: webdriver.Chrome) -> None:
    """Garante que há uma sessão logada.

    Se o perfil do Chrome já estiver autenticado, não faz nada. Caso contrário
    abre a tela de login e espera você entrar manualmente e apertar ENTER.
    Nunca preenche usuário/senha nem tenta burlar 2FA/CAPTCHA.
    """
    driver.get("https://www.instagram.com/")
    polite_sleep(2, 3)
    if is_logged_in(driver):
        log.info("Sessão já autenticada detectada. Pulando login manual.")
        return

    driver.get("https://www.instagram.com/accounts/login/")
    polite_sleep(2, 3)
    print("\n" + "=" * 70)
    print("FAÇA LOGIN MANUALMENTE NA JANELA DO NAVEGADOR.")
    print("Se aparecer verificação em duas etapas ou CAPTCHA, resolva normalmente.")
    print("Quando estiver logado, volte aqui e pressione ENTER.")
    print("=" * 70 + "\n")
    input("Pressione ENTER após concluir o login... ")

    if not is_logged_in(driver):
        log.warning("Não foi possível confirmar o login automaticamente. "
                    "Prosseguindo mesmo assim — verifique na janela do navegador.")


def open_profile(driver: webdriver.Chrome) -> None:
    """Abre o perfil alvo e espera o grid de publicações aparecer."""
    log.info(f"Abrindo perfil: {PROFILE_URL}")
    driver.get(PROFILE_URL)
    try:
        WebDriverWait(driver, ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "article"))
        )
    except TimeoutException:
        log.warning("Timeout esperando o grid do perfil. "
                    "Verifique se o perfil é público e se você está logado.")
    polite_sleep(*POLITE_DELAY)


def get_post_urls(driver: webdriver.Chrome) -> list[str]:
    """Lista as URLs de publicações atualmente carregadas na página, na
    ordem do feed (mais recente primeiro), sem repetições."""
    links = driver.find_elements(By.CSS_SELECTOR, POST_LINK_SELECTOR)
    urls: list[str] = []
    for link in links:
        try:
            url = normalize_post_url(link.get_attribute("href"))
        except StaleElementReferenceException:
            continue  # o Instagram reciclou esse elemento do DOM
        if url and url not in urls:
            urls.append(url)
    return urls


def scroll_feed(driver: webdriver.Chrome) -> None:
    """Rola o feed um pouco (cerca de 85% da altura da janela) e espera o
    Instagram carregar mais posts. Rolagens pequenas e frequentes disparam o
    carregamento melhor do que saltar direto para o fim da página."""
    driver.execute_script(
        "window.scrollBy({top: Math.max(window.innerHeight * 0.85, 650), "
        "left: 0, behavior: 'instant'});"
    )
    polite_sleep(*SCROLL_DELAY)


def looks_rate_limited(driver: webdriver.Chrome) -> bool:
    """Detecta se o Instagram mostra aviso de limite de requisições ou
    bloqueio temporário (procura frases típicas no texto da página).
    Nunca levanta erro: na dúvida, retorna False."""
    markers = (
        "tente novamente mais tarde", "try again later",
        "aguarde um momento", "please wait a few minutes",
        "we limit how often", "limitamos a frequência",
        "unusual activity", "atividade incomum",
    )
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
    except (NoSuchElementException, *BROWSER_ERRORS):
        return False
    return any(m in body for m in markers)


def recover_feed(driver: webdriver.Chrome, attempt: int) -> None:
    """Tenta destravar um feed que parou de carregar posts.

    Faz isso em três passos: (1) espera, com tempo crescente a cada tentativa
    (maior se houver sinal de rate-limit); (2) rola várias vezes seguidas;
    (3) se nem links de posts sobraram na tela, recarrega o perfil.
    "Sem posts novos" nunca é interpretado como fim do perfil.
    """
    backoff = min(150, 20 * attempt)
    if looks_rate_limited(driver):
        backoff = max(backoff, 90)
        log.warning(f"[Recuperação #{attempt}] Sinal de rate-limit detectado.")
    log.warning(f"[Recuperação #{attempt}] Feed sem avanço. Aguardando {backoff}s.")
    time.sleep(backoff)

    for _ in range(RECOVERY_SCROLLS):
        scroll_feed(driver)

    if not get_post_urls(driver):
        log.warning("[Recuperação] Nenhum post na tela; recarregando o perfil.")
        try:
            open_profile(driver)
        except TimeoutException:
            log.warning("[Recuperação] O perfil demorou a carregar; seguindo mesmo assim.")


# ==========================================================================
# POSICIONAMENTO MANUAL
# ==========================================================================

def get_scroll_position(driver: webdriver.Chrome) -> int:
    """Retorna a posição vertical atual da rolagem da página, em pixels."""
    return int(driver.execute_script(
        "return window.pageYOffset || document.documentElement.scrollTop || 0;"
    ))


def get_urls_above_viewport(driver: webdriver.Chrome) -> set[str]:
    """Retorna as URLs dos posts que já passaram do topo da tela (rolados
    para cima, fora de vista). São os posts mais recentes que o ponto onde
    você parou, ou seja, fora da janela desejada."""
    hrefs = driver.execute_script(
        """
        return Array.from(document.querySelectorAll(arguments[0]))
            .filter(a => a.getBoundingClientRect().bottom <= 0)
            .map(a => a.href);
        """,
        POST_LINK_SELECTOR,
    )
    urls = (normalize_post_url(h) for h in (hrefs or []))
    return {u for u in urls if u}


def wait_for_manual_positioning(driver: webdriver.Chrome) -> set[str]:
    """Espera você rolar o feed até o primeiro post a coletar.

    Observa a posição da rolagem a cada MANUAL_POLL_INTERVAL segundos. Quando
    ela fica parada por MANUAL_IDLE_SECONDS segundos seguidos, considera o
    posicionamento concluído. DICA: deixe o primeiro post desejado visível
    na tela antes de parar de rolar.

    Retorna o conjunto de URLs acima da tela (posts mais recentes que o ponto
    escolhido), que a coleta vai ignorar sem abrir.
    """
    log.info("=" * 70)
    log.info("POSICIONAMENTO MANUAL")
    log.info(f"Role o feed até o PRIMEIRO post da janela "
             f"({TARGET_START.date()} a {TARGET_END.date()}).")
    log.info(f"Ao parar de rolar por {MANUAL_IDLE_SECONDS}s, a coleta começa.")
    log.info("=" * 70)

    last_position = get_scroll_position(driver)
    last_change = time.monotonic()
    last_log = time.monotonic()

    while True:
        time.sleep(MANUAL_POLL_INTERVAL)
        position = get_scroll_position(driver)
        if position != last_position:
            last_position = position
            last_change = time.monotonic()

        idle = time.monotonic() - last_change
        if time.monotonic() - last_log >= 5:
            log.info(f"[Posicionamento] parado há {idle:.0f}s "
                     f"(coleta inicia após {MANUAL_IDLE_SECONDS}s).")
            last_log = time.monotonic()

        if idle >= MANUAL_IDLE_SECONDS:
            skip_urls = get_urls_above_viewport(driver)
            log.info(f"[Posicionamento] concluído. {len(skip_urls)} posts acima do "
                     "ponto escolhido serão ignorados sem serem abertos.")
            return skip_urls


# ==========================================================================
# POSICIONAMENTO AUTOMÁTICO (--modo auto)
# ==========================================================================

class RateEstimator:
    """Estimativa (média móvel exponencial) de publicações por hora,
    recalibrada a cada amostra real. Serve para decidir o tamanho dos saltos."""

    def __init__(self, initial_rate: float = INITIAL_RATE_POSTS_PER_HOUR, alpha: float = 0.4):
        self.rate = initial_rate
        self.alpha = alpha

    def update(self, posts_delta: int, hours_delta: float) -> None:
        if hours_delta <= 0 or posts_delta <= 0:
            return
        observed = posts_delta / hours_delta
        self.rate = max(0.5, self.alpha * observed + (1 - self.alpha) * self.rate)
        log.info(f"[Taxa] observada={observed:.2f} posts/h | suavizada={self.rate:.2f} posts/h")

    def estimate_posts_between(self, date_a: datetime, date_b: datetime) -> int:
        hours = abs((date_a - date_b).total_seconds()) / 3600.0
        return max(1, int(round(hours * self.rate)))


def load_posts_until(driver: webdriver.Chrome, target_count: int) -> int:
    """Rola o feed até haver pelo menos `target_count` posts no DOM. Para se
    o feed parar de crescer por SEARCH_STAGNANT_LIMIT rolagens (sem assumir
    que isso é o fim do perfil). Retorna quantos posts há no DOM."""
    count = len(get_post_urls(driver))
    scrolls = 0
    stagnant = 0
    while count < target_count and scrolls < SEARCH_MAX_SCROLLS:
        driver.execute_script(
            "window.scrollBy({top: document.body.scrollHeight, left: 0, behavior: 'instant'});"
        )
        polite_sleep(0.8, 1.6)
        scrolls += 1

        new_count = len(get_post_urls(driver))
        stagnant = stagnant + 1 if new_count <= count else 0
        count = max(count, new_count)
        if stagnant >= SEARCH_STAGNANT_LIMIT:
            log.warning("[Busca] O feed não cresceu após várias rolagens; "
                        "isso NÃO é tratado como fim do perfil.")
            break
        if scrolls % 20 == 0:
            log.info(f"[Busca] {count} posts carregados após {scrolls} rolagens "
                     f"(alvo: {target_count})")
    return count


def read_post_datetime(driver: webdriver.Chrome, url: str) -> Optional[datetime]:
    """Abre o post em uma aba nova só para ler a data (tag <time>), fecha a
    aba e devolve o datetime no fuso da pesquisa. None se não conseguir ler."""
    main_handle = driver.current_window_handle
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", url)
        driver.switch_to.window(driver.window_handles[-1])
        time_el = WebDriverWait(driver, ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "time[datetime]"))
        )
        iso = time_el.get_attribute("datetime")
        return parse_instagram_datetime(iso) if iso else None
    except (TimeoutException, NoSuchElementException, ValueError) as e:
        log.warning(f"[Busca] Falha ao ler a data de {url}: {e}")
        return None
    finally:
        try:
            if driver.current_window_handle != main_handle:
                driver.close()
            driver.switch_to.window(main_handle)
        except BROWSER_ERRORS as e:
            log.warning(f"Não foi possível fechar a aba do post: {e}")
        polite_sleep(0.5, 1.2)


def find_start_index(driver: webdriver.Chrome) -> int:
    """Descobre o índice (posição no feed, 0 = mais recente) do primeiro post
    com data <= TARGET_END.

    Fase 1 (galope): pula de N em N posts, com N estimado pela taxa de
    publicação, até achar um post com data <= TARGET_END. A menos de
    SEARCH_FINE_DAYS dias do alvo os saltos ficam menores.
    Fase 2 (bisseção): refina entre o último post ainda mais recente que o
    alvo e o primeiro já dentro dele, até achar o limite exato.
    """
    estimator = RateEstimator()
    date_cache: dict[str, datetime] = {}

    def date_at(index: int) -> Optional[datetime]:
        """Data do post na posição `index` (com cache e novas tentativas)."""
        urls = get_post_urls(driver)
        if index >= len(urls):
            return None
        url = urls[index]
        if url in date_cache:
            return date_cache[url]
        for attempt in range(1, POST_RETRY_ATTEMPTS + 1):
            found = read_post_datetime(driver, url)
            if found is not None:
                date_cache[url] = found
                return found
            if attempt < POST_RETRY_ATTEMPTS:
                time.sleep(POST_RETRY_BACKOFF * attempt)
        return None

    log.info(f"=== BUSCA AUTOMÁTICA (alvo: primeiro post <= {TARGET_END.isoformat()}) ===")

    if load_posts_until(driver, 1) < 1:
        raise BrowserUnresponsiveError("Nenhum post carregou no perfil.")
    first_date = date_at(0)
    if first_date is None:
        raise RuntimeError("Não foi possível ler a data do primeiro post do perfil.")
    log.info(f"Post mais recente do perfil: {first_date.isoformat()}")
    if first_date <= TARGET_END:
        log.info("O post mais recente já está dentro (ou antes) da janela.")
        return 0

    # ---- Fase 1: galope
    low = 0                                   # data(low) > TARGET_END
    last_idx, last_date = 0, first_date
    idx = estimator.estimate_posts_between(first_date, TARGET_END)
    high: Optional[int] = None                # data(high) <= TARGET_END

    for round_n in range(SEARCH_MAX_GALLOP_ROUNDS):
        available = load_posts_until(driver, idx + 1)
        probe = min(idx, available - 1)
        if probe <= low:
            raise BrowserUnresponsiveError(
                f"O feed não carregou além do índice {low} durante o galope.")

        sampled = date_at(probe)
        if sampled is None:
            probe -= 1  # tenta o vizinho
            sampled = date_at(probe) if probe > low else None
            if sampled is None:
                raise RuntimeError(f"Não foi possível ler datas perto do índice {probe}.")

        log.info(f"[Galope #{round_n}] índice={probe} data={sampled.date()} "
                 f"(janela: {TARGET_START.date()} a {TARGET_END.date()})")
        estimator.update(abs(probe - last_idx),
                         abs((last_date - sampled).total_seconds()) / 3600.0)

        if sampled <= TARGET_END:
            high = probe
            break

        low = probe
        last_idx, last_date = probe, sampled
        days_to_target = (sampled - TARGET_END).total_seconds() / 86400.0
        remaining = estimator.estimate_posts_between(sampled, TARGET_END)
        if days_to_target <= SEARCH_FINE_DAYS:
            jump = remaining                   # perto do alvo: sem salto mínimo grande
            log.info(f"[Aproximação fina] faltam {days_to_target:.1f} dias; salto de {jump}.")
        else:
            jump = max(remaining, SEARCH_MIN_JUMP)
            log.info(f"[Galope] faltam {days_to_target:.1f} dias; salto de {jump}.")
        idx = probe + jump
    else:
        raise RuntimeError("A busca exponencial não convergiu "
                           "(a data alvo é anterior ao início do perfil?).")

    # ---- Fase 2: bisseção
    log.info("=== BUSCA AUTOMÁTICA: bisseção do limite temporal ===")
    lo, hi = low, high
    while hi - lo > 1:
        mid = (lo + hi) // 2
        mid_date = date_at(mid)
        if mid_date is None:
            for neighbor in (mid - 1, mid + 1):
                if lo < neighbor < hi:
                    neighbor_date = date_at(neighbor)
                    if neighbor_date is not None:
                        mid, mid_date = neighbor, neighbor_date
                        break
        if mid_date is None:
            raise RuntimeError(f"Não foi possível ler a data do índice {mid}.")

        log.info(f"[Bisseção] lo={lo} hi={hi} mid={mid} data={mid_date.date()}")
        if mid_date <= TARGET_END:
            hi = mid
        else:
            lo = mid

    log.info(f"[Alvo encontrado] índice={hi}")
    return hi


def auto_position(driver: webdriver.Chrome) -> set[str]:
    """Equivalente automático de wait_for_manual_positioning.

    Acha o primeiro post da janela, leva o feed até ele e devolve o conjunto
    de URLs mais recentes que ele (que a coleta ignora sem abrir)."""
    start_index = find_start_index(driver)
    urls = get_post_urls(driver)
    skip_urls = set(urls[:start_index])

    if start_index < len(urls):
        code = shortcode_from_url(urls[start_index])
        driver.execute_script(
            """
            const a = Array.from(document.querySelectorAll(arguments[0]))
                .find(x => x.href.includes(arguments[1]));
            if (a) a.scrollIntoView({block: 'center'});
            """,
            POST_LINK_SELECTOR, code,
        )
        polite_sleep(1.0, 2.0)

    log.info(f"[Busca automática] concluída. {len(skip_urls)} posts mais recentes "
             "que a janela serão ignorados sem serem abertos.")
    return skip_urls


# ==========================================================================
# EXTRAÇÃO DE UMA PUBLICAÇÃO
# ==========================================================================

def read_attribute(driver: webdriver.Chrome, css_selector: str, attribute: str) -> str:
    """Lê um atributo do primeiro elemento que casa com o seletor CSS.
    Retorna "" se o elemento não existir."""
    try:
        return driver.find_element(By.CSS_SELECTOR, css_selector).get_attribute(attribute) or ""
    except NoSuchElementException:
        return ""


def read_text(driver: webdriver.Chrome, css_selector: str) -> str:
    """Lê o texto visível do primeiro elemento que casa com o seletor CSS.
    Retorna "" se o elemento não existir."""
    try:
        return driver.find_element(By.CSS_SELECTOR, css_selector).text.strip()
    except NoSuchElementException:
        return ""


def detect_media_type(driver: webdriver.Chrome, url: str) -> tuple[str, Optional[int]]:
    """Descobre o tipo da mídia da página do post aberta no navegador.

    Retorna (tipo, quantidade): "video" para reels/tv e posts com <video>;
    "carousel" quando há botão de próximo (quantidade None, pois o HTML
    público não permite contar com confiança); caso contrário "image".
    """
    if "/reel/" in url or "/tv/" in url:
        return "video", 1
    if driver.find_elements(
        By.CSS_SELECTOR, "button[aria-label='Próximo'], button[aria-label='Next']"
    ):
        return "carousel", None
    if driver.find_elements(By.TAG_NAME, "video"):
        return "video", 1
    return "image", 1


def extract_post(driver: webdriver.Chrome, url: str) -> Optional[PostRecord]:
    """Abre o post em uma aba nova, extrai os dados e fecha a aba.

    Dados lidos: data/hora (tag <time>, convertida para o fuso da pesquisa),
    curtidas/comentários/legenda (meta og:description; legenda também pelo
    <h1>) e tipo de mídia. Retorna None em falhas recuperáveis (página não
    carregou, sem data); falhas do próprio Chrome sobem como exceção para o
    programa principal salvar o progresso e encerrar.
    """
    main_handle = driver.current_window_handle
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", url)
        driver.switch_to.window(driver.window_handles[-1])

        time_el = WebDriverWait(driver, ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "time[datetime]"))
        )
        iso = time_el.get_attribute("datetime")
        if not iso:
            raise ValueError("Elemento <time> sem atributo datetime.")
        published = parse_instagram_datetime(iso)

        likes, comments, og_caption = parse_og_description(
            read_attribute(driver, "meta[property='og:description']", "content")
        )
        if likes is None:  # alternativa: contador visível na página
            likes = parse_count_text(read_text(driver, "section a[href*='liked_by'] span"))

        caption = read_text(driver, "h1") or og_caption
        post_type, media_count = detect_media_type(driver, url)

        return PostRecord(
            post_id=shortcode_from_url(url) or url,
            url=url,
            post_type=post_type,
            media_count=media_count,
            likes=likes,
            comments=comments,
            caption=caption,
            date=published.strftime("%Y-%m-%d"),
            time=published.strftime("%H:%M:%S"),
            datetime_iso=published.isoformat(),
            collected_at=datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        )

    except (TimeoutException, NoSuchElementException, ValueError) as e:
        log.warning(f"Falha ao ler {url}: {e}")
        return None

    finally:
        # Sempre volta para a aba principal, mesmo após falha.
        try:
            if driver.current_window_handle != main_handle:
                driver.close()
            driver.switch_to.window(main_handle)
        except BROWSER_ERRORS as e:
            log.warning(f"Não foi possível fechar a aba do post: {e}")
        polite_sleep(0.6, 1.4)


def fetch_with_retries(driver: webdriver.Chrome, url: str) -> Optional[PostRecord]:
    """Chama `extract_post` até POST_RETRY_ATTEMPTS vezes, esperando um tempo
    crescente entre as tentativas. Retorna None se todas falharem."""
    for attempt in range(1, POST_RETRY_ATTEMPTS + 1):
        record = extract_post(driver, url)
        if record is not None:
            return record
        log.warning(f"[Post] tentativa {attempt}/{POST_RETRY_ATTEMPTS} falhou: {url}")
        if attempt < POST_RETRY_ATTEMPTS:
            time.sleep(POST_RETRY_BACKOFF * attempt)
    return None


# ==========================================================================
# COLETA
# ==========================================================================

def collect_posts(
    driver: webdriver.Chrome,
    results: dict[str, PostRecord],
    processed_urls: set[str],
    abandoned_urls: set[str],
    skip_urls: set[str],
) -> None:
    """Percorre o feed coletando os posts da janela [TARGET_START, TARGET_END].

    Em cada rodada: lista as URLs na tela, abre as que ainda não foram
    processadas, salva o progresso e rola o feed. Regras importantes:
      - uma URL só vira "processada" depois de lida com sucesso;
      - posts que falham em vários ciclos são abandonados e registrados no
        checkpoint (nada some em silêncio);
      - "sem URLs novas" NÃO é fim do perfil: aciona `recover_feed`, e se o
        feed continuar travado levanta BrowserUnresponsiveError (o programa
        salva e encerra);
      - o fim só é confirmado após END_CONFIRMATION_COUNT posts seguidos
        anteriores a TARGET_START (um único post antigo pode ser fixado).

    Os dicionários/conjuntos recebidos são atualizados no lugar. Retorna
    normalmente apenas quando o fim da janela foi confirmado.
    """
    log.info(f"=== COLETA: até confirmar posts anteriores a {TARGET_START.date()} ===")

    older_streak = 0          # posts seguidos anteriores à janela
    stagnant_rounds = 0       # rodadas seguidas sem nenhum avanço
    recoveries = 0            # recuperações seguidas sem avanço
    failure_cycles: dict[str, int] = {}
    seen_in_dom: set[str] = set()

    while True:
        dom_urls = get_post_urls(driver)
        new_in_dom = [u for u in dom_urls if u not in seen_in_dom]
        seen_in_dom.update(dom_urls)
        pending = [u for u in dom_urls if u not in processed_urls and u not in skip_urls]

        log.info(f"[Coleta] na tela={len(dom_urls)} | pendentes={len(pending)} | "
                 f"coletados={len(results)}")

        # Chegar a URLs novas no DOM também conta como avanço (importante
        # ao retomar uma coleta, quando o script rola até a região que
        # ainda falta sem abrir nada).
        progressed = bool(new_in_dom)

        for url in pending:
            record = fetch_with_retries(driver, url)

            if record is None:
                cycles = failure_cycles.get(url, 0) + 1
                failure_cycles[url] = cycles
                if cycles >= ABANDON_AFTER_CYCLES:
                    log.error(f"[Abandonado] {url} falhou em {cycles} ciclos de tentativas.")
                    abandoned_urls.add(url)
                    processed_urls.add(url)
                    progressed = True
                continue

            progressed = True
            processed_urls.add(url)
            published = datetime.fromisoformat(record.datetime_iso)

            if published < TARGET_START:
                older_streak += 1
                log.info(f"[Anterior à janela] {published.date()} "
                         f"({older_streak}/{END_CONFIRMATION_COUNT})")
                if older_streak >= END_CONFIRMATION_COUNT:
                    finish_collection(results, processed_urls, abandoned_urls)
                    return
                continue

            older_streak = 0
            if published > TARGET_END:
                continue  # mais recente que a janela

            results[record.post_id] = record
            log.info(f"[Coletado #{len(results)}] {record.date} {record.time} | "
                     f"curtidas={record.likes} | comentários={record.comments} | "
                     f"{record.post_type} | {record.url}")

        save_progress(results, processed_urls, abandoned_urls)

        if progressed:
            stagnant_rounds = 0
            recoveries = 0
        else:
            stagnant_rounds += 1
            log.warning(f"[Sem avanço] rodada {stagnant_rounds}/{RECOVERY_EVERY_ROUNDS} "
                        "(isto NÃO é tratado como fim da coleta).")

        scroll_feed(driver)

        if stagnant_rounds >= RECOVERY_EVERY_ROUNDS:
            recoveries += 1
            if recoveries > MAX_RECOVERIES:
                raise BrowserUnresponsiveError(
                    f"Feed sem avanço após {MAX_RECOVERIES} recuperações seguidas."
                )
            recover_feed(driver, recoveries)
            stagnant_rounds = 0


def finish_collection(
    results: dict[str, PostRecord], processed_urls: set[str], abandoned_urls: set[str]
) -> None:
    """Encerra a coleta: salva tudo e registra no log uma auditoria
    (período realmente coberto x esperado e URLs abandonadas)."""
    save_progress(results, processed_urls, abandoned_urls)

    if results:
        dates = [r.date for r in results.values()]
        log.info(f"[Auditoria] coletado: {min(dates)} a {max(dates)} | "
                 f"esperado: {TARGET_START.date()} a {TARGET_END.date()}")
    if abandoned_urls:
        log.warning(f"[Auditoria] {len(abandoned_urls)} URLs foram abandonadas "
                    f"(veja {CHECKPOINT_FILE}); considere coletá-las depois.")
    log.info(f"=== COLETA CONCLUÍDA: {len(results)} publicações ===")


# ==========================================================================
# PROGRAMA PRINCIPAL
# ==========================================================================

def main(mode: str = DEFAULT_MODE) -> None:
    """Ponto de entrada: carrega o que já foi coletado, abre o Chrome, faz
    login, posiciona o feed e executa a coleta.

    `mode` escolhe como o feed é posicionado na janela de datas:
      - "manual": você rola o feed até o primeiro post e para;
      - "auto":   o script acha o primeiro post da janela sozinho.

    Se o Chrome travar, ocorrer outro erro ou o usuário apertar Ctrl+C, o
    progresso é salvo e o programa encerra. Rodar de novo retoma do
    CSV/checkpoint.
    """
    results = load_results_csv()
    processed_urls, abandoned_urls = load_checkpoint()
    log.info(f"Modo de busca: {mode.upper()}")

    driver = None
    try:
        driver = build_driver()
        login(driver)
        open_profile(driver)

        if mode == MODE_AUTO:
            skip_urls = auto_position(driver)
        else:
            skip_urls = wait_for_manual_positioning(driver)

        collect_posts(driver, results, processed_urls, abandoned_urls, skip_urls)

    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário — salvando progresso...")
        save_progress(results, processed_urls, abandoned_urls)

    except Exception as e:
        log.exception(f"[Erro] {type(e).__name__}: {e}")
        save_progress(results, processed_urls, abandoned_urls)
        log.error("Progresso salvo. Rode de novo para retomar.")

    finally:
        safe_quit_driver(driver)

    log.info("Programa encerrado.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta posts públicos do Instagram dentro de uma janela de datas.")
    parser.add_argument(
        "--modo", choices=[MODE_MANUAL, MODE_AUTO], default=DEFAULT_MODE,
        help="manual: você rola o feed até o primeiro post da janela; "
             "auto: o script localiza o ponto inicial sozinho "
             f"(padrão: {DEFAULT_MODE}).")
    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args().modo)
