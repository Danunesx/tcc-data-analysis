# Scraper de publicações do Instagram

Coletor de publicações públicas no Instagram, desenvolvido para a monografia *Análise exploratória de dados em publicações no Instagram no período da Copa do Mundo 2026* (Engenharia de Computação, UFES).

O script percorre o feed do perfil dentro de uma janela de datas, abre cada publicação em uma aba nova, extrai os dados públicos e grava tudo em um arquivo CSV. A coleta pode ser interrompida e retomada sem perder o progresso.

## Sumário

- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Configuração](#configuração)
- [Como executar](#como-executar)
- [Modos de busca](#modos-de-busca)
- [Dados gerados](#dados-gerados)
- [Retomada e controle de qualidade](#retomada-e-controle-de-qualidade)
- [Limitações conhecidas](#limitações-conhecidas)
- [Termos de uso e ética](#termos-de-uso-e-ética)
- [Estrutura do projeto](#estrutura-do-projeto)

## Requisitos

- Python 3.9 ou superior
- Google Chrome instalado
- Uma conta do Instagram para fazer login manualmente
- Dependências Python listadas em `requirements.txt`

O ChromeDriver é baixado automaticamente pelo Selenium (versão 4.6 ou superior), então não precisa ser instalado à mão.

## Instalação

```bash
# 1. (opcional) criar um ambiente virtual
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# 2. instalar as dependências
pip install -r requirements.txt
```

## Configuração

Antes de rodar, edite a seção `CONFIGURAÇÃO` no início do `script_v2.py`.

### 1. Janela de datas

```python
TARGET_START = datetime(2026, 11, 17, 10, 59, 59, tzinfo=TIMEZONE)  # mais antigo
TARGET_END   = datetime(2026, 11, 17, 12, 59, 59, tzinfo=TIMEZONE)  # mais recente
```

Os valores que vêm no script são uma janela curta de teste (2 horas). Para reproduzir a coleta da monografia, use o período analisado, de **19/05/2026 a 20/08/2026**:

```python
TARGET_START = datetime(2026, 5, 19, 0, 0, 0, tzinfo=TIMEZONE)
TARGET_END   = datetime(2026, 8, 20, 23, 59, 59, tzinfo=TIMEZONE)
```

O Instagram informa as datas em UTC. O script converte para o horário de Brasília (UTC-3) para decidir a que dia cada publicação pertence.

### 2. Perfil do Chrome

```python
CHROME_USER_DATA_DIR = r"..\ChromeSeleniumProfile"
CHROME_PROFILE_DIRECTORY = "Profile (com Instagram logado)"
```
- `CHROME_USER_DATA_DIR` é a pasta `User Data` (tudo que vem antes do nome do perfil);
- `CHROME_PROFILE_DIRECTORY` é só o nome da pasta do perfil, por exemplo `Profile 2`.

Se preferir não usar perfil dedicado, coloque `None` nas duas variáveis. Nesse caso será preciso fazer login a cada execução.

> Feche todas as janelas do Chrome normal antes de rodar. O Chrome não permite dois processos no mesmo perfil.

### 3. Outros parâmetros (opcionais)

| Parâmetro | Padrão | Função |
|---|---|---|
| `POLITE_DELAY` | 1,5 a 3,0 s | Pausa aleatória entre ações |
| `SCROLL_DELAY` | 1,8 a 3,2 s | Pausa após cada rolagem |
| `MANUAL_IDLE_SECONDS` | 30 | Tempo de rolagem parada para iniciar a coleta (modo manual) |
| `ELEMENT_TIMEOUT` | 15 s | Espera por um elemento na página |
| `PAGE_LOAD_TIMEOUT` | 45 s | Espera máxima pelo carregamento de uma página |
| `POST_RETRY_ATTEMPTS` | 3 | Tentativas seguidas para ler um post |
| `ABANDON_AFTER_CYCLES` | 3 | Ciclos de falha antes de abandonar um post |
| `END_CONFIRMATION_COUNT` | 3 | Posts seguidos anteriores à janela para encerrar |

## Como executar

```bash
python script_v2.py                # modo manual (padrão)
python script_v2.py --modo manual
python script_v2.py --modo auto
```

O fluxo de uma execução é:

1. **Login:** o script abre o Instagram. Se o perfil do Chrome já estiver autenticado, ele pula essa etapa. Caso contrário, você faz o login manualmente na janela e aperta ENTER no terminal.
2. **Posicionamento do feed:** o script leva o feed até o primeiro post da janela (veja os [modos de busca](#modos-de-busca)).
3. **Coleta:** para cada post, abre uma aba nova, extrai os dados, fecha a aba e rola o feed.
4. **Encerramento:** a coleta termina ao encontrar posts anteriores a `TARGET_START`. O script salva tudo e registra uma auditoria no log, comparando o período coberto com o esperado.

## Modos de busca

| Modo | Como funciona | Quando usar |
|---|---|---|
| `manual` (padrão) | Você rola o feed até o primeiro post a coletar. Quando a rolagem fica parada por `MANUAL_IDLE_SECONDS` segundos, a coleta começa dali. | Quando você já sabe aproximadamente onde está a data inicial. |
| `auto` | O script localiza sozinho o ponto inicial: estima a taxa de publicações por hora, avança por saltos exponenciais e refina com busca binária (bisseção) lendo a data de alguns posts. | Quando a janela está longe do topo do feed e você não quer rolar à mão. |

Depois do posicionamento, os dois modos usam exatamente a mesma coleta.

## Dados gerados

### `database.csv`

Codificação UTF-8 com BOM (abre corretamente no Excel). Uma linha por publicação, ordenadas da mais recente para a mais antiga.

| Coluna | Descrição |
|---|---|
| `post_id` | Código curto da URL (*shortcode*), usado como identificador único |
| `url` | Endereço da publicação |
| `post_type` | `image`, `video` ou `carousel` |
| `media_count` | Quantidade de mídias. Fica vazio em carrosséis, pois não dá para contar com confiança |
| `likes` | Curtidas. Fica vazio se o Instagram ocultar ou se não for possível interpretar o valor |
| `comments` | Comentários |
| `caption` | Legenda, incluindo hashtags |
| `date` | Data no formato `AAAA-MM-DD`, no fuso UTC-3 |
| `time` | Horário no formato `HH:MM:SS`, no fuso UTC-3 |
| `datetime_iso` | Data e hora em ISO 8601 com fuso (ex.: `2026-05-03T14:22:11-03:00`) |
| `collected_at` | Momento em que os dados foram coletados |

Como curtidas e comentários mudam com o tempo, `collected_at` registra a que momento cada contagem corresponde. Se a coleta for feita em dias diferentes, vale considerar essa coluna na análise, já que posts coletados mais tarde tiveram mais tempo para acumular interações.

Valores abreviados do Instagram (como `12,3 mil` e `1,2M`) são convertidos para inteiros. Quando a conversão é ambígua, o campo fica vazio, para não gravar um valor possivelmente errado.

### Outros arquivos

| Arquivo | Conteúdo |
|---|---|
| `checkpoint.json` | URLs já processadas e URLs abandonadas após falhas repetidas |
| `scraper.log` | Registro das operações (níveis INFO, WARNING e ERROR) |

## Retomada e controle de qualidade

- **Retomada:** o CSV e o checkpoint são salvos a cada rodada, com escrita atômica (arquivo temporário seguido de substituição), o que evita arquivos corrompidos se o computador travar. Se o Chrome travar ou ocorrer um erro, o script salva e encerra. Basta rodar de novo para continuar de onde parou. `Ctrl+C` também salva o progresso.
- **Tentativas:** falhas na leitura de um post acionam até 3 tentativas com espera crescente. Se o problema persistir após 3 ciclos, a URL é registrada como abandonada.
- **Recuperação do feed:** se o feed deixa de carregar novas publicações, o script tenta rolagens adicionais e esperas. Após 6 recuperações seguidas sem avanço, ele salva e encerra.
- **Posts fixados:** o fim da coleta só é declarado depois de 3 posts consecutivos anteriores à janela, evitando que um post fixado no topo do perfil interrompa a coleta cedo demais.
- **Auditoria:** ao final, o log compara o período realmente coletado com o esperado e lista as URLs abandonadas, que podem ser coletadas depois.

> **Nova coleta com outra janela:** o script sempre recarrega o CSV e o checkpoint existentes. Para começar uma coleta do zero ou com outro período, mova ou apague `database.csv` e `checkpoint.json` antes de rodar. Caso contrário, os novos posts serão somados aos antigos.

## Limitações conhecidas

- **O HTML do Instagram muda com frequência.** Se algum campo vier vazio em todos os posts, os seletores CSS podem precisar de ajuste (veja o início do módulo e as funções `extract_post` e `detect_media_type`).
- **O Instagram limita requisições.** O script usa pausas aleatórias e esperas explícitas, mas bloqueios temporários ainda podem ocorrer.
- **Curtidas ocultas.** Posts com curtidas ocultas ficam com `likes` vazio.
- **Carrosséis.** O número de páginas não é contado (`media_count` vazio).
- **Dados dinâmicos.** As contagens refletem o momento da coleta, não o momento da publicação.
- **Duração dos vídeos.** Este script não coleta a duração dos vídeos. Na monografia, ela foi obtida em uma etapa separada, a partir das URLs.

## Termos de uso e ética

Leia com atenção antes de usar.

- A coleta de dados automatizada pode contrariar os Termos de Uso do Instagram. O uso deste script é de responsabilidade de quem o executa.
- O script coleta apenas dados públicos de um perfil institucional (contagens, legenda e data) e não busca dados pessoais de terceiros.
- O login é sempre manual. O script nunca preenche usuário e senha nem tenta burlar 2FA ou CAPTCHA.
- Por outro lado, o script **reduz os sinais de automação do navegador** (argumento `--disable-blink-features=AutomationControlled`, remoção da propriedade `navigator.webdriver` e das opções `enable-automation`). Isso faz com que a automação seja menos visível para a plataforma, e esse ponto deve ser levado em conta ao avaliar o uso.
- Use o script com finalidade educativa e científica, com intervalos entre as requisições, e observe as normas éticas da sua instituição.
- Se for compartilhar a base coletada, considere o que ela contém (legendas e nomes de perfis em colaboração) e evite usos que possam expor ou descontextualizar o perfil analisado.

## Estrutura do projeto

```
.
├── script_v2.py        # scraper
├── requirements.txt    # dependências
├── README.md
│
├── database.csv    # gerado: dados coletados
├── checkpoint.json     # gerado: progresso da coleta
└── scraper.log         # gerado: log de execução
```

## Referência

NUNES, Daniel Pereira. *Análise exploratória de dados em publicações no Instagram no período da Copa do Mundo 2026*. Monografia (Bacharelado em Engenharia de Computação) – Universidade Federal do Espírito Santo, São Mateus, 2026. Orientador: Dr. Pedro Henrique Vieira de Oliveira Azevedo.
