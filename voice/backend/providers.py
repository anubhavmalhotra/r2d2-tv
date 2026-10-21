"""Offline native Transformers providers. Loading is explicit, never import-time.

Paths must be independently qualified immutable local model snapshots. No URLs,
remote Python, hosted API fallback, payload files, logging or cache retention.
This library alone does not enforce process/GPU deadlines or kernel egress rules.
"""
import hashlib
import os
from pathlib import Path
import time

if __package__:
    from .controller import Refused, bounded_text
    from .local_qwen import SYSTEM, prompt_for_phase
else:
    from controller import Refused, bounded_text
    from local_qwen import SYSTEM, prompt_for_phase


def local_snapshot(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise Refused('MODEL_CONFIG')
    return str(path.resolve())


class NativeQwenASR:
    # Set only by independent controlled-trial qualification, not by load success.
    qualified = False

    def __init__(self, path, *, torch_module=None, processor_class=None, model_class=None):
        self.path = local_snapshot(path)
        self.torch = torch_module
        self.processor_class, self.model_class = processor_class, model_class
        self.processor = self.model = None

    def load(self):
        if self.model is not None:
            raise Refused('MODEL_ALREADY_LOADED')
        if self.torch is None:
            import torch
            from transformers import AutoProcessor, AutoModelForMultimodalLM
            self.torch = torch
            self.processor_class, self.model_class = AutoProcessor, AutoModelForMultimodalLM
        if not self.torch.cuda.is_available():
            raise Refused('CUDA_UNAVAILABLE')
        self.processor = self.processor_class.from_pretrained(
            self.path, local_files_only=True, trust_remote_code=False)
        self.model = self.model_class.from_pretrained(
            self.path, local_files_only=True, trust_remote_code=False,
            dtype=self.torch.bfloat16, attn_implementation='sdpa').to('cuda:0').eval()

    def transcribe(self, pcm, *, rate, timeout_s):
        if (self.model is None or type(pcm) not in (bytes, bytearray) or
                not 640 <= len(pcm) <= 320000 or len(pcm) % 2 or rate != 16000 or
                type(timeout_s) not in (float, int) or not 0 < timeout_s <= 8):
            raise Refused('ASR_UNAVAILABLE')
        import numpy as np
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        values = generated = None
        try:
            values = self.processor.apply_transcription_request(
                audio=audio, sampling_rate=rate, language='English').to('cuda:0', self.torch.bfloat16)
            with self.torch.inference_mode():
                ids = self.model.generate(**values, max_new_tokens=192, do_sample=False,
                                          max_time=timeout_s, use_cache=True)
            generated = ids[:, values['input_ids'].shape[1]:]
            text = self.processor.decode(generated, return_format='transcription_only')
            if isinstance(text, list) and len(text) == 1:
                text = text[0]
            if type(text) is str:
                text = text.strip()
            if text == '':
                # Silence is a valid answer, not a worker fault.
                return ''
            if not bounded_text(text, 1024, 4096):
                raise Refused('ASR_OUTPUT_INVALID')
            return text
        finally:
            audio.fill(0)
            audio = values = generated = None


class NativeQwenIntent:
    def __init__(self, path, *, torch_module=None, tokenizer_class=None, model_class=None,
                 system_prompt=None):
        self.path = local_snapshot(path)
        self.torch = torch_module
        self.tokenizer_class, self.model_class = tokenizer_class, model_class
        self.tokenizer = self.model = None
        self.system_prompt = None
        self.prompt_sha256 = None
        if system_prompt is None:
            system_prompt = prompt_for_phase(os.environ.get('R2D2_PROMPT_PHASE'))
        self.configure_prompt(system_prompt)

    def configure_prompt(self, system_prompt, *, phase=None):
        if phase is not None and system_prompt != prompt_for_phase(phase):
            raise Refused('MODEL_CONFIG')
        if type(system_prompt) is not str or not 1 <= len(system_prompt.encode('utf-8')) <= 8192:
            raise Refused('MODEL_CONFIG')
        self.system_prompt = system_prompt
        self.prompt_sha256 = hashlib.sha256(system_prompt.encode('utf-8')).hexdigest()

    def load(self):
        if self.model is not None:
            raise Refused('MODEL_ALREADY_LOADED')
        if self.torch is None:
            import torch
            from transformers import AutoTokenizer, AutoModelForCausalLM
            self.torch = torch
            self.tokenizer_class, self.model_class = AutoTokenizer, AutoModelForCausalLM
        if not self.torch.cuda.is_available():
            raise Refused('CUDA_UNAVAILABLE')
        self.tokenizer = self.tokenizer_class.from_pretrained(
            self.path, local_files_only=True, trust_remote_code=False)
        self.model = self.model_class.from_pretrained(
            self.path, local_files_only=True, trust_remote_code=False,
            dtype=self.torch.bfloat16, attn_implementation='sdpa').to('cuda:0').eval()

    def interpret(self, transcript, *, timeout_s):
        if (self.model is None or not bounded_text(transcript, 1024, 4096) or
                type(timeout_s) not in (float, int) or not 0 < timeout_s <= 5):
            raise Refused('MODEL_UNAVAILABLE')
        values = raw = generated = None
        try:
            raw = self.tokenizer.apply_chat_template(
                [{'role': 'system', 'content': self.system_prompt}, {'role': 'user', 'content': transcript}],
                tokenize=False, add_generation_prompt=True)
            values = self.tokenizer(raw, return_tensors='pt').to('cuda:0')
            if values['input_ids'].shape[1] > 2048:
                raise Refused('MODEL_SCHEMA')
            with self.torch.inference_mode():
                ids = self.model.generate(**values, max_new_tokens=192, do_sample=False,
                                          max_time=timeout_s, use_cache=True)
            generated = ids[0][values['input_ids'].shape[1]:]
            text = self.tokenizer.decode(generated, skip_special_tokens=True)
            if type(text) is not str or len(text.encode('utf-8')) > 2048:
                raise Refused('MODEL_SCHEMA')
            return text
        finally:
            transcript = values = raw = generated = None
