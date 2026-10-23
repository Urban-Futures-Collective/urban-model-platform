import logging
import os
import threading
from threading import Timer
from typing import List, Optional

import yaml
from pydantic import ValidationError
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.providers_config import (
    ProcessConfig,
    ProviderConfig,
    ProvidersConfig,
    ProvidersSnapshot,
)

logger = logging.getLogger(__name__)


class _ConfigFileHandler(FileSystemEventHandler):
    DEBOUNCE_DELAY = 0.5  # 500ms

    def __init__(self, adapter: ProvidersPort, config_path: str, config_filename: str):
        self.adapter = adapter
        self.config_path = os.path.abspath(config_path)
        self.config_filename = config_filename
        self._reload_timer = None

    def on_any_event(self, event):
        logger.info("File event: %s on %s", event.event_type, event.src_path)

        if event.is_directory:
            return
        src_path = str(event.src_path)
        # Robust checks for configmap updates (Kubernetes)
        if (
            src_path == self.config_path
            or src_path.endswith(self.config_filename)
            or "..data" in src_path
        ):
            self._debounced_reload()

    def _debounced_reload(self):
        if self._reload_timer:
            self._reload_timer.cancel()
        self._reload_timer = Timer(self.DEBOUNCE_DELAY, self.adapter.load_providers)
        self._reload_timer.start()


class ProviderConfigFileAdapter(ProvidersPort):
    def __init__(self, config_path: str):
        self._config_path = config_path
        # Guards the snapshot swap; readers take the reference under the lock.
        self._lock = threading.RLock()
        self._snapshot = ProvidersSnapshot()

        self.load_providers()

    def start_file_watcher(self):
        observer = PollingObserver()
        config_dir = os.path.dirname(self._config_path)
        config_filename = os.path.basename(self._config_path)

        handler = _ConfigFileHandler(self, self._config_path, config_filename)

        observer.schedule(handler, path=config_dir, recursive=False)
        observer.start()

        self._observer = observer

    def stop_file_watcher(self):
        if hasattr(self, "_observer"):
            self._observer.stop()
            self._observer.join()

    def _atomic_update(self, new_providers: ProvidersConfig):
        """Swap in a new snapshot; on error the previous one stays active."""
        snapshot = ProvidersSnapshot(
            [provider.model_copy(deep=True) for provider in new_providers.providers]
        )
        with self._lock:
            self._snapshot = snapshot

    def _current(self) -> ProvidersSnapshot:
        with self._lock:
            return self._snapshot

    def load_providers(self):
        logger.info(f"(Re)Loading providers from {self._config_path}")

        try:
            with open(self._config_path, encoding="UTF-8") as file:
                content = yaml.safe_load(file)
                if content:
                    validated = ProvidersConfig(**content)
                    self._atomic_update(validated)
                    count = len(validated.providers)
                    names = [p.name for p in validated.providers]
                    logger.info(
                        "Providers (re)loaded: %d provider(s): %s",
                        count,
                        names,
                    )
                else:
                    # yaml.safe_load returns None for an empty file.
                    # This is a valid but likely unintended state — warn loudly
                    # so operators notice that no providers are available.
                    logger.warning(
                        "Providers file is empty (%s) — no providers configured, process routes will 404",
                        self._config_path,
                    )
        except FileNotFoundError:
            logger.error("Providers file not found: %s", self._config_path)
        except yaml.YAMLError as parse_error:
            logger.error("Failed to parse providers file: %s", parse_error)
        except ValidationError as validation_error:
            logger.error("Validation error in providers file: %s", validation_error)
        except Exception as unexpected_error:
            logger.error("Unexpected error loading providers: %s", unexpected_error)

    def get_providers(self) -> List[ProviderConfig]:
        return self._current().providers

    def get_provider(self, provider_name: str) -> Optional[ProviderConfig]:
        return self._current().get_provider(provider_name)

    def get_process_config(
        self, provider_name: str, process_id: str
    ) -> Optional[ProcessConfig]:
        return self._current().get_process_config(provider_name, process_id)

    def list_providers(self) -> List[str]:
        return self._current().provider_names()

    def get_processes(self, provider_name: str) -> List[str]:
        return self._current().get_processes(provider_name)

    def check_process_availability(self, provider_name: str, process_id: str) -> bool:
        return self._current().check_process_availability(provider_name, process_id)
