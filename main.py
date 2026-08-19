"""VoiceCrew Lite — a low-memory Kivy Android voice and text AI client.

All app logic intentionally lives in this file so the repository stays small and
GitHub Actions builds it without a multi-module Python package.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.utils import platform

try:
    from pypdf import PdfReader
except ImportError:  # The interface remains usable until the Android dependency is packaged.
    PdfReader = None

APP_TITLE = "VoiceCrew"
MAX_CHATS = 30
MAX_MESSAGES_PER_CHAT = 120
MAX_HISTORY_MESSAGES = 10
MAX_KNOWLEDGE_FILES = 8
MAX_FILE_TEXT = 60_000
MAX_KNOWLEDGE_CONTEXT = 3_500
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".csv", ".json", ".docx", ".py"}
WORD_RE = re.compile(r"[\wÀ-ÿঀ-৿]+", re.UNICODE)

DEFAULT_STATE: dict[str, Any] = {
    "settings": {"provider": "OpenAI", "model": "gpt-4o-mini", "speak_answers": True},
    "chats": [],
    "knowledge": [],
}

SYSTEM_PROMPT = """তুমি VoiceCrew, একটি সহায়ক AI agent। প্রাথমিক ভাষা বাংলা;
ব্যবহারকারী যে ভাষায় লিখবে বা বলবে, সেই ভাষায় স্বাভাবিকভাবে উত্তর দাও। উত্তর স্পষ্ট,
সংক্ষিপ্ত এবং কথ্য ভাষায় দাও যেন তা voice-এ শোনা সহজ হয়। knowledge parameter দেওয়া হলে
শুধু প্রাসঙ্গিক তথ্য ব্যবহার করো; তথ্য না পেলে অনুমানকে সত্য হিসেবে উপস্থাপন করো না।"""


def atomic_json(path: Path, data: Any) -> None:
    """Write app state atomically to avoid losing chats if Android closes the app."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".voicecrew-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class SecureKeyStore:
    """Keeps API keys encrypted with Android Keystore; desktop fallback is preview-only."""

    ALIAS = "voicecrew.lite.aes.gcm"

    def __init__(self, folder: Path) -> None:
        self.path = folder / "keys.json"
        try:
            self.values = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.values = {}

    def set(self, provider: str, value: str) -> None:
        if not value:
            return
        if platform == "android":
            saved = "android:" + self._encrypt(value)
        else:
            # This branch is only for local desktop preview. Android always uses Keystore.
            saved = "preview:" + base64.urlsafe_b64encode(value.encode()).decode()
        self.values[provider] = saved
        atomic_json(self.path, self.values)

    def get(self, provider: str) -> str:
        saved = str(self.values.get(provider, ""))
        try:
            if saved.startswith("android:") and platform == "android":
                return self._decrypt(saved[8:])
            if saved.startswith("preview:"):
                return base64.urlsafe_b64decode(saved[8:]).decode()
        except Exception:
            return ""
        return ""

    def has(self, provider: str) -> bool:
        return bool(self.get(provider))

    def clear(self, provider: str) -> None:
        self.values.pop(provider, None)
        atomic_json(self.path, self.values)

    def _key(self):
        from jnius import autoclass

        KeyStore = autoclass("java.security.KeyStore")
        KeyGenerator = autoclass("javax.crypto.KeyGenerator")
        KeyProperties = autoclass("android.security.keystore.KeyProperties")
        KeyGenBuilder = autoclass("android.security.keystore.KeyGenParameterSpec$Builder")
        store = KeyStore.getInstance("AndroidKeyStore")
        store.load(None)
        if not store.containsAlias(self.ALIAS):
            generator = KeyGenerator.getInstance("AES", "AndroidKeyStore")
            purposes = KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
            spec = KeyGenBuilder(self.ALIAS, purposes).setBlockModes(
                KeyProperties.BLOCK_MODE_GCM
            ).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setKeySize(256).build()
            generator.init(spec)
            generator.generateKey()
        return store.getKey(self.ALIAS, None)

    def _encrypt(self, value: str) -> str:
        from jnius import autoclass

        Cipher = autoclass("javax.crypto.Cipher")
        cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, self._key())
        return base64.urlsafe_b64encode(bytes(cipher.getIV()) + bytes(cipher.doFinal(value.encode()))).decode()

    def _decrypt(self, value: str) -> str:
        from jnius import autoclass

        raw = base64.urlsafe_b64decode(value.encode())
        Cipher = autoclass("javax.crypto.Cipher")
        GCMParameterSpec = autoclass("javax.crypto.spec.GCMParameterSpec")
        cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, self._key(), GCMParameterSpec(128, raw[:12]))
        return bytes(cipher.doFinal(raw[12:])).decode()


class LocalStore:
    """Low-overhead JSON persistence for settings, chats, and compact knowledge text."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "state.json"
        self.keys = SecureKeyStore(folder)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data = {
            "settings": {**DEFAULT_STATE["settings"], **data.get("settings", {})},
            "chats": data.get("chats", []),
            "knowledge": data.get("knowledge", []),
        }
        if not self.data["chats"]:
            self.new_chat()

    def save(self) -> None:
        atomic_json(self.path, self.data)

    @property
    def settings(self) -> dict[str, Any]:
        return self.data["settings"]

    def update_settings(self, provider: str, model: str, speak: bool) -> None:
        self.settings.update({"provider": provider, "model": model or "gpt-4o-mini", "speak_answers": bool(speak)})
        self.save()

    def new_chat(self) -> dict[str, Any]:
        chat = {"id": uuid.uuid4().hex, "title": "নতুন চ্যাট", "updated": time.time(), "messages": []}
        self.data["chats"].insert(0, chat)
        self.data["chats"] = self.data["chats"][:MAX_CHATS]
        self.save()
        return chat

    def chats(self) -> list[dict[str, Any]]:
        return sorted(self.data["chats"], key=lambda chat: chat.get("updated", 0), reverse=True)

    def chat(self, chat_id: str) -> dict[str, Any] | None:
        return next((item for item in self.data["chats"] if item["id"] == chat_id), None)

    def delete_chat(self, chat_id: str) -> str:
        self.data["chats"] = [chat for chat in self.data["chats"] if chat["id"] != chat_id]
        if not self.data["chats"]:
            return self.new_chat()["id"]
        self.save()
        return self.chats()[0]["id"]

    def add_message(self, chat_id: str, role: str, text: str) -> None:
        chat = self.chat(chat_id)
        if chat is None:
            return
        clean = text.strip()[:8_000]
        chat["messages"].append({"role": role, "content": clean})
        chat["messages"] = chat["messages"][-MAX_MESSAGES_PER_CHAT:]
        chat["updated"] = time.time()
        if role == "user" and chat["title"] == "নতুন চ্যাট":
            compact = " ".join(clean.split())
            chat["title"] = compact[:36] + ("…" if len(compact) > 36 else "")
        self.save()


class KnowledgeAgent:
    """A no-embedding, keyword-ranked knowledge parameter for low-RAM phones."""

    def __init__(self, store: LocalStore) -> None:
        self.store = store

    def import_file(self, path: str) -> dict[str, Any]:
        source = Path(path)
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError("সমর্থিত ফাইল: PDF, TXT, MD, CSV, JSON, DOCX, PY")
        if len(self.store.data["knowledge"]) >= MAX_KNOWLEDGE_FILES:
            raise ValueError(f"সর্বোচ্চ {MAX_KNOWLEDGE_FILES}টি knowledge file রাখা যাবে")
        if not source.is_file() or source.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("ফাইলটি পাওয়া যায়নি অথবা 8 MB-এর বেশি")
        text = self._read(source, suffix).strip()
        if not text:
            raise ValueError("পাঠযোগ্য text পাওয়া যায়নি; scanned PDF হলে OCR প্রয়োজন")
        item = {"id": uuid.uuid4().hex[:10], "name": source.name[:80], "format": suffix[1:].upper(), "text": text[:MAX_FILE_TEXT]}
        self.store.data["knowledge"].append(item)
        self.store.save()
        return item

    def remove(self, item_id: str) -> None:
        self.store.data["knowledge"] = [item for item in self.store.data["knowledge"] if item["id"] != item_id]
        self.store.save()

    def context_for(self, question: str) -> str:
        terms = [word.lower() for word in WORD_RE.findall(question) if len(word) > 1]
        if not terms:
            return ""
        weights = Counter(terms)
        ranked: list[tuple[int, str, str]] = []
        for item in self.store.data["knowledge"]:
            text = item.get("text", "")
            for start in range(0, len(text), 1100):
                chunk = text[start:start + 1300]
                low = chunk.lower()
                score = sum(low.count(term) * weight for term, weight in weights.items())
                if score:
                    ranked.append((score, item["name"], chunk))
        ranked.sort(key=lambda item: item[0], reverse=True)
        result, remaining = [], MAX_KNOWLEDGE_CONTEXT
        for _score, name, chunk in ranked[:4]:
            part = f"[Knowledge: {name}]\n{chunk.strip()}"
            if len(part) > remaining:
                part = part[:remaining]
            if part:
                result.append(part)
                remaining -= len(part)
            if remaining <= 0:
                break
        return "\n\n---\n\n".join(result)

    @staticmethod
    def _read(path: Path, suffix: str) -> str:
        if suffix == ".pdf":
            if PdfReader is None:
                raise ValueError("PDF reader dependency পাওয়া যায়নি")
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        if suffix == ".docx":
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            with zipfile.ZipFile(path) as archive:
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            return "\n".join("".join(node.text or "" for node in paragraph.iter(f"{namespace}t")) for paragraph in root.iter(f"{namespace}p"))
        return path.read_text(encoding="utf-8", errors="replace")


class LLMError(RuntimeError):
    pass


class PrimaryLLM:
    """The exact provider, model name, and API key configured in Settings handle every request."""

    def __init__(self, store: LocalStore, knowledge: KnowledgeAgent) -> None:
        self.store, self.knowledge = store, knowledge

    def answer(self, history: list[dict[str, str]], question: str) -> str:
        provider = self.store.settings["provider"]
        model = self.store.settings["model"].strip()
        key = self.store.keys.get(provider)
        if not key:
            raise LLMError("Settings থেকে API key সংরক্ষণ করুন")
        knowledge_parameter = self.knowledge.context_for(question)
        messages = history[-MAX_HISTORY_MESSAGES:] + [{"role": "user", "content": question}]
        if provider == "Google AI Studio":
            return self._gemini(key, model, messages, knowledge_parameter)
        return self._openai_compatible(provider, key, model, messages, knowledge_parameter)

    @staticmethod
    def _system(knowledge_parameter: str) -> str:
        return SYSTEM_PROMPT + (f"\n\nKnowledge parameter:\n{knowledge_parameter}" if knowledge_parameter else "")

    @staticmethod
    def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=75) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            try:
                data = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                data = {}
            detail = data.get("error", data.get("message", "অজানা সমস্যা"))
            if isinstance(detail, dict):
                detail = detail.get("message", str(detail))
            raise LLMError(f"LLM service error ({error.code}): {detail}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise LLMError(f"নেটওয়ার্ক সমস্যা: {error}") from error
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise LLMError("LLM service বৈধ JSON উত্তর দেয়নি") from error

    def _openai_compatible(self, provider: str, key: str, model: str, messages: list[dict[str, str]], context: str) -> str:
        base = "https://api.openai.com/v1/chat/completions" if provider == "OpenAI" else "https://api.deepseek.com/chat/completions"
        data = self._post(base, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, {
            "model": model,
            "messages": [{"role": "system", "content": self._system(context)}] + messages,
            "temperature": 0.6,
            "max_tokens": 900,
        })
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, TypeError, IndexError, AttributeError) as error:
            raise LLMError("LLM-এর উত্তর বোঝা যায়নি") from error

    def _gemini(self, key: str, model: str, messages: list[dict[str, str]], context: str) -> str:
        contents = [{"role": "model" if msg["role"] == "assistant" else "user", "parts": [{"text": msg["content"]}]} for msg in messages]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        data = self._post(url, {"Content-Type": "application/json"}, {
            "systemInstruction": {"parts": [{"text": self._system(context)}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 900},
        })
        try:
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"]).strip()
        except (KeyError, TypeError, IndexError, AttributeError) as error:
            raise LLMError("Google AI Studio-এর উত্তর বোঝা যায়নি") from error


if platform == "android":
    from jnius import PythonJavaClass, autoclass, java_method

    class _TTSInit(PythonJavaClass):
        __javainterfaces__ = ["android/speech/tts/TextToSpeech$OnInitListener"]
        __javacontext__ = "app"

        def __init__(self, callback):
            super().__init__()
            self.callback = callback

        @java_method("(I)V")
        def onInit(self, status):
            self.callback(status)

    class _SpeechEvents(PythonJavaClass):
        __javainterfaces__ = ["android/speech/RecognitionListener"]
        __javacontext__ = "app"

        def __init__(self, on_text, on_error):
            super().__init__()
            self.on_text, self.on_error = on_text, on_error

        @java_method("(Landroid/os/Bundle;)V")
        def onReadyForSpeech(self, _): pass
        @java_method("()V")
        def onBeginningOfSpeech(self): pass
        @java_method("(F)V")
        def onRmsChanged(self, _): pass
        @java_method("([B)V")
        def onBufferReceived(self, _): pass
        @java_method("()V")
        def onEndOfSpeech(self): pass
        @java_method("(Landroid/os/Bundle;)V")
        def onPartialResults(self, _): pass
        @java_method("(ILandroid/os/Bundle;)V")
        def onEvent(self, _, __): pass

        @java_method("(I)V")
        def onError(self, code):
            messages = {1: "নেটওয়ার্ক সমস্যা", 2: "নেটওয়ার্ক সমস্যা", 5: "আরেকটি voice request চলছে", 6: "কথা শোনা যায়নি", 7: "কথা বোঝা যায়নি", 8: "Speech service নেই", 9: "Microphone permission প্রয়োজন"}
            self.on_error(messages.get(code, "Voice input সম্পন্ন হয়নি"))

        @java_method("(Landroid/os/Bundle;)V")
        def onResults(self, results):
            SpeechRecognizer = autoclass("android.speech.SpeechRecognizer")
            values = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            if values is not None and values.size() > 0:
                self.on_text(str(values.get(0)))
            else:
                self.on_error("কথা বোঝা যায়নি")


class AndroidVoice:
    """Uses device STT/TTS to avoid keeping audio models in the app's RAM."""

    def __init__(self) -> None:
        self.tts = self.recognizer = self.listener = self.init_listener = None
        self.ready, self.pending = False, ""
        if platform == "android":
            TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
            from android import mActivity
            self.init_listener = _TTSInit(self._ready)
            self.tts = TextToSpeech(mActivity, self.init_listener)

    def _ready(self, status: int) -> None:
        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        if status != TextToSpeech.SUCCESS:
            return
        Locale = autoclass("java.util.Locale")
        self.tts.setLanguage(Locale.forLanguageTag("bn-BD"))
        try:
            for voice in self.tts.getVoices().toArray():
                label = str(voice.getName()).lower()
                if "female" in label or "woman" in label:
                    self.tts.setVoice(voice)
                    break
        except Exception:
            pass
        self.tts.setSpeechRate(0.94)
        self.tts.setPitch(1.05)
        self.ready = True
        if self.pending:
            text, self.pending = self.pending, ""
            self.speak(text)

    def speak(self, text: str) -> None:
        if platform != "android" or not text.strip():
            return
        if not self.ready:
            self.pending = text[:5000]
            return
        TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
        self.tts.speak(text[:5000], TextToSpeech.QUEUE_FLUSH, None, "voicecrew-reply")

    def listen(self, on_text, on_error) -> None:
        if platform != "android":
            on_error("Voice chat Android device-এ ব্যবহার করুন")
            return
        SpeechRecognizer = autoclass("android.speech.SpeechRecognizer")
        RecognizerIntent = autoclass("android.speech.RecognizerIntent")
        Intent = autoclass("android.content.Intent")
        from android import mActivity
        if not SpeechRecognizer.isRecognitionAvailable(mActivity):
            on_error("এই ডিভাইসে speech recognition service নেই")
            return
        if self.recognizer is None:
            self.recognizer = SpeechRecognizer.createSpeechRecognizer(mActivity)
        self.listener = _SpeechEvents(on_text, on_error)
        self.recognizer.setRecognitionListener(self.listener)
        intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "bn-BD")
        intent.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, False)
        self.recognizer.startListening(intent)

    def stop(self) -> None:
        if self.tts is not None:
            self.tts.stop()

    def close(self) -> None:
        if self.recognizer is not None:
            self.recognizer.destroy()
        if self.tts is not None:
            self.tts.shutdown()


class KnowledgePicker:
    REQUEST_CODE = 41414

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.callback = self.error = None
        self.bound = False

    def choose(self, callback, error) -> None:
        self.callback, self.error = callback, error
        if platform == "android":
            from jnius import autoclass
            from android import activity, mActivity
            Intent = autoclass("android.content.Intent")
            intent = Intent(Intent.ACTION_OPEN_DOCUMENT)
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            intent.setType("*/*")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            activity.bind(on_activity_result=self._activity_result)
            self.bound = True
            mActivity.startActivityForResult(intent, self.REQUEST_CODE)
        else:
            from kivy.uix.filechooser import FileChooserListView
            from kivy.uix.popup import Popup
            box = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(8))
            picker = FileChooserListView(filters=["*.pdf", "*.txt", "*.md", "*.csv", "*.json", "*.docx", "*.py"])
            button = Button(text="এই ফাইলটি যোগ করুন", size_hint_y=None, height=dp(48))
            popup = Popup(title="Knowledge file", content=box, size_hint=(.94, .9))
            button.bind(on_release=lambda *_: (popup.dismiss(), callback(picker.selection[0])) if picker.selection else None)
            box.add_widget(picker); box.add_widget(button); popup.open()

    def _activity_result(self, request, result, intent) -> None:
        if request != self.REQUEST_CODE:
            return
        self._unbind()
        try:
            Activity = autoclass("android.app.Activity")
            if result != Activity.RESULT_OK or intent is None:
                return
            uri = intent.getData()
            if uri is None:
                return
            from android import mActivity
            name = re.sub(r"[^\w.()-]+", "_", str(uri.getLastPathSegment()).split(":")[-1]) or "knowledge.txt"
            target = self.destination / f"{int(time.time() * 1000)}_{name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            source = mActivity.getContentResolver().openInputStream(uri)
            with target.open("wb") as output:
                buffer = bytearray(8192)
                while True:
                    count = source.read(buffer)
                    if count == -1:
                        break
                    if count:
                        output.write(buffer[:count])
            source.close()
            self.callback(str(target))
        except Exception as exc:
            self.error(f"ফাইল যোগ করা যায়নি: {exc}")
        finally:
            self.callback = self.error = None

    def _unbind(self) -> None:
        if self.bound and platform == "android":
            from android import activity
            activity.unbind(on_activity_result=self._activity_result)
        self.bound = False


class ChatRow(BoxLayout):
    role = StringProperty("assistant")
    text = StringProperty("")


class Root(BoxLayout):
    pass


class VoiceCrewApp(App):
    current_chat_id = StringProperty("")
    current_title = StringProperty("নতুন চ্যাট")
    status = StringProperty("প্রস্তুত")
    working = BooleanProperty(False)

    def build(self):
        return Builder.load_file(str(Path(__file__).with_name("ui.kv")))

    def on_start(self):
        folder = self.user_data_dir if platform != "android" else self._android_folder()
        self.store = LocalStore(Path(folder))
        self.knowledge = KnowledgeAgent(self.store)
        self.voice = AndroidVoice()
        self.picker = KnowledgePicker(Path(folder) / "imports")
        self.current_chat_id = self.store.chats()[0]["id"]
        self.refresh_chat()

    def _android_folder(self) -> str:
        from android.storage import app_storage_path
        return app_storage_path()

    def on_stop(self):
        if hasattr(self, "voice"):
            self.voice.close()

    def page(self, name: str):
        return self.root.ids.screens.get_screen(name).children[0]

    def show_chat(self):
        self.root.ids.screens.current = "chat"
        self.refresh_chat()

    def show_settings(self):
        settings = self.page("settings")
        settings.ids.provider.text = self.store.settings["provider"]
        settings.ids.model.text = self.store.settings["model"]
        settings.ids.api_key.text = ""
        settings.ids.key_hint.text = "এই provider-এর key সংরক্ষিত" if self.store.keys.has(settings.ids.provider.text) else "এখনও API key সংরক্ষণ করা হয়নি"
        settings.ids.speak.active = bool(self.store.settings.get("speak_answers", True))
        self.refresh_knowledge()
        self.root.ids.screens.current = "settings"

    def provider_changed(self):
        if not hasattr(self, "store"):
            return
        settings = self.page("settings")
        provider = settings.ids.provider.text
        defaults = {"OpenAI": "gpt-4o-mini", "DeepSeek": "deepseek-chat", "Google AI Studio": "gemini-2.5-flash"}
        settings.ids.model.text = defaults.get(provider, "")
        settings.ids.api_key.text = ""
        settings.ids.key_hint.text = "এই provider-এর key সংরক্ষিত" if self.store.keys.has(provider) else "এই provider-এর API key দিন"

    def save_settings(self):
        page = self.page("settings")
        provider, model = page.ids.provider.text, page.ids.model.text.strip()
        if not model:
            self.status = "Model name প্রয়োজন"
            return
        self.store.update_settings(provider, model, page.ids.speak.active)
        if page.ids.api_key.text.strip():
            self.store.keys.set(provider, page.ids.api_key.text.strip())
            page.ids.api_key.text = ""
        self.status = "Settings সংরক্ষণ করা হয়েছে"
        self.show_chat()

    def clear_key(self):
        page = self.page("settings")
        self.store.keys.clear(page.ids.provider.text)
        page.ids.api_key.text = ""
        page.ids.key_hint.text = "API key মুছে ফেলা হয়েছে"

    def new_chat(self):
        self.current_chat_id = self.store.new_chat()["id"]
        self.refresh_chat()
        self.status = "নতুন চ্যাট তৈরি হয়েছে"

    def open_sidebar(self):
        from kivy.factory import Factory
        drawer = Factory.ChatDrawer()
        rows = drawer.ids.chat_list
        for chat in self.store.chats():
            item = Button(text=chat["title"], size_hint_y=None, height=dp(46), halign="left", valign="middle", text_size=(dp(214), None), background_normal="", background_color=(.18,.18,.18,1), color=(.95,.95,.95,1))
            item.bind(on_release=lambda _, chat_id=chat["id"], modal=drawer: self.select_chat(chat_id, modal))
            rows.add_widget(item)
        drawer.open()

    def select_chat(self, chat_id: str, drawer=None):
        self.current_chat_id = chat_id
        if drawer:
            drawer.dismiss()
        self.refresh_chat()

    def delete_current_chat(self):
        self.current_chat_id = self.store.delete_chat(self.current_chat_id)
        self.refresh_chat()
        self.status = "চ্যাট মুছে ফেলা হয়েছে"

    def refresh_chat(self):
        if not hasattr(self, "store") or not self.current_chat_id:
            return
        chat = self.store.chat(self.current_chat_id)
        if not chat:
            return
        self.current_title = chat["title"]
        container = self.page("chat").ids.messages
        container.clear_widgets()
        if not chat["messages"]:
            container.add_widget(Label(text="✦\n\nকীভাবে সাহায্য করতে পারি?", font_size=dp(20), halign="center", valign="middle", color=(.9,.9,.9,1), text_size=(dp(310), None), size_hint_y=None, height=dp(380)))
        else:
            for message in chat["messages"]:
                container.add_widget(ChatRow(role=message["role"], text=message["content"]))
        Clock.schedule_once(lambda _: setattr(self.page("chat").ids.scroll, "scroll_y", 0), .05)

    def send(self):
        if self.working:
            return
        composer = self.page("chat").ids.composer
        question = composer.text.strip()
        if not question:
            self.status = "লিখুন অথবা microphone চাপুন"
            return
        composer.text = ""
        chat = self.store.chat(self.current_chat_id)
        history = list(chat["messages"]) if chat else []
        self.store.add_message(self.current_chat_id, "user", question)
        self.refresh_chat()
        self.working, self.status = True, "উত্তর তৈরি হচ্ছে…"
        threading.Thread(target=self._request, args=(self.current_chat_id, history, question), daemon=True).start()

    def _request(self, chat_id: str, history: list[dict[str, str]], question: str):
        try:
            answer = PrimaryLLM(self.store, self.knowledge).answer(history, question)
            self.finish(chat_id, answer, "")
        except LLMError as exc:
            self.finish(chat_id, "", str(exc))
        except Exception as exc:
            self.finish(chat_id, "", f"সমস্যা হয়েছে: {exc}")

    @mainthread
    def finish(self, chat_id: str, answer: str, error: str):
        self.working = False
        if error:
            self.status = error
            return
        self.store.add_message(chat_id, "assistant", answer)
        self.status = f"{self.store.settings['provider']} উত্তর দিয়েছে"
        if chat_id == self.current_chat_id:
            self.refresh_chat()
        if self.store.settings.get("speak_answers"):
            self.voice.speak(answer)

    def voice_input(self):
        if self.working:
            self.status = "বর্তমান উত্তর শেষ হওয়া পর্যন্ত অপেক্ষা করুন"
            return
        if platform == "android":
            from android.permissions import Permission, check_permission, request_permissions
            if not check_permission(Permission.RECORD_AUDIO):
                request_permissions([Permission.RECORD_AUDIO], self._permission_done)
                return
        self.listen_now()

    def _permission_done(self, _permissions, grants):
        if grants and grants[0]:
            self.listen_now()
        else:
            self.status = "Voice input-এর জন্য Microphone permission দিন"

    def listen_now(self):
        self.status = "শুনছি…"
        self.voice.listen(self.voice_text, self.voice_error)

    @mainthread
    def voice_text(self, text: str):
        self.page("chat").ids.composer.text = text
        self.status = "শোনা কথাটি পাঠানো হচ্ছে…"
        self.send()

    @mainthread
    def voice_error(self, message: str):
        self.status = message

    def stop_voice(self):
        self.voice.stop()
        self.status = "Voice উত্তর থামানো হয়েছে"

    def choose_knowledge(self):
        self.picker.choose(self.import_knowledge, self.knowledge_error)

    @mainthread
    def import_knowledge(self, path: str):
        try:
            item = self.knowledge.import_file(path)
            self.status = f"Knowledge যোগ হয়েছে: {item['name']}"
            self.refresh_knowledge()
        except ValueError as exc:
            self.status = str(exc)
        finally:
            # Android SAF first copies a URI into app-private imports; desktop-selected source
            # files belong to the user and must never be removed by the app.
            if platform == "android":
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    @mainthread
    def knowledge_error(self, message: str):
        self.status = message

    def refresh_knowledge(self):
        if not hasattr(self, "store"):
            return
        box = self.page("settings").ids.knowledge_list
        box.clear_widgets()
        items = self.store.data["knowledge"]
        if not items:
            box.add_widget(Label(text="কোনো knowledge file নেই", size_hint_y=None, height=dp(32), color=(.62,.62,.62,1)))
        for item in items:
            row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(6))
            row.add_widget(Label(text=f"{item['name']}  ·  {item['format']}", halign="left", valign="middle", text_size=(dp(230), None), color=(.92,.92,.92,1)))
            remove = Button(text="মুছুন", size_hint_x=None, width=dp(62), background_normal="", background_color=(.38,.14,.14,1))
            remove.bind(on_release=lambda _, item_id=item["id"]: self.remove_knowledge(item_id))
            row.add_widget(remove); box.add_widget(row)

    def remove_knowledge(self, item_id: str):
        self.knowledge.remove(item_id)
        self.refresh_knowledge()
        self.status = "Knowledge file মুছে ফেলা হয়েছে"


if __name__ == "__main__":
    VoiceCrewApp().run()
