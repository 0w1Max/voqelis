# Horizon Small ONNX — изолированный пилот пунктуации

**Статус:** эксперимент; не является production-изменением.

Кандидат: [Horizon-Labs/punctuation-restoration-small v1.1](https://huggingface.co/Horizon-Labs/punctuation-restoration-small), revision \`ee31fdf\` (2026-10-09). Apache-2.0; ONNX quantized ~= 268 MB, tokenizer ~= 34 MB. Из-за особенностей mmBERT обработчик выбирает предсказанную пунктуацию **с последнего подтокена каждого слова**, не с первого. Текст ASR никогда не заменяется перефразированным выводом.

## Граница эксперимента

- Код: \`tools/horizon_model_fetch.py\`, \`tools/horizon_onnx_probe.py\`.
- Данные: \`/var/lib/voqelis/horizon-onnx-pilot/\` — отдельный каталог с ограниченным доступом, который не используется ботом.
- Загрузка: только \`config.json\`, \`tokenizer.json\`, \`onnx/model_quantized.onnx\` по pinned revision; без \`model.safetensors\`, \`transformers\`, \`torch\` и изменений \`.env\`.
- Загрузчик требует явного \`--accept-download\`, минимум 700MB свободного места, проверяет HTTPS+пределы размеров и записывает SHA256 manifest. Для особо строгого audit необходимо дополнительно сверить digest с авторитетным манифестом релиза (предоставляемый upstream сайт не указывает его в модельной карточке).
- Только короткие непунктуированные русские тексты. При потере/усечении токенов — ошибка вместо частичного вывода. Знаки \`-\` отключены во избежание слияния или искажения слов; время \`13:00\` не превращается в якорь для пунктуации.
- Для сравнения с RUPunct на том же корпусе нужно запускать базовую модель **отдельным** процессом, не держать оба весовых набора одновременно в 2 GB RAM.
- Фраза «Друг общался раздраженно был в своих мыслях» — обязательный пилотный случай. Не принимать обещаний улучшения из общего бенчмарка модели без измерения на нашем материале.

## Выполнение

Из чистой отдельной ветки/после PR approval — без изменений production сервиса:

1. Выполнить \`ruff check --isolated --no-cache --ignore UP017,UP041 src tests\`, \`compileall\`, \`pytest\`.
2. Запустить загрузчик под \`voqelis\`:
   \`PYTHONPATH=src .venv/bin/python -m tools.horizon_model_fetch --dest /var/lib/voqelis/horizon-onnx-pilot --accept-download\`.
3. Запустить оценку с отдельным systemd-run unit: \`MemoryMax=900M\`, \`MemorySwapMax=0\`, \`CPUQuota=50%\`, \`RuntimeMaxSec=180s\`, \`NoNewPrivileges=yes\`, \`PYTHONPATH=/opt/voqelis/src\`; использовать \`-m tools.horizon_onnx_probe --model-dir /var/lib/voqelis/horizon-onnx-pilot\`.
4. В отчёте обязательно сохранить: outputs, p50 latency, peak RSS, exit code, 0 изменённых файлов Git, active voqelis.service.
5. При несовместимости ONNX ops или tokenizer — не заменять production и не выполнять \`pip install\` в общей virtualenv.

**Недостаточно для rollout:** набор из 12 crafted примеров позволяет отсечь кандидата, но не доказывает качество. Нужен слепой ручной gold на 30–50 реальных обезличенных аудио/транскрипциях (comma precision/recall/F1, period F1, boundary split/merge, exact lexical preservation, latency, RSS). Дальше отдельный worker + feature flag и осторожная приёмка в Telegram.
