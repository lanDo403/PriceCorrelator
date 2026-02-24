# Архитектура проекта

Проект состоит из независимых модулей с четкими зонами ответственности:

- получение метаданных события Polymarket;
- получение потоковых цен через RTDS;
- анализ лага между источниками;
- CLI-обвязка для запуска мониторинга и вывода результата.

Связанные файлы кода:

- `src/price_correlator/config.py`
- `src/price_correlator/models.py`
- `src/price_correlator/event_client.py`
- `src/price_correlator/rtds_client.py`
- `src/price_correlator/lag_analyzer.py`
- `src/price_correlator/monitor.py`
- `src/price_correlator/cli.py`
