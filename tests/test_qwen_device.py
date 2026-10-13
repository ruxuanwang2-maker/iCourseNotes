import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.ai.qwen_transcriber import QwenTranscriber


class DeviceTests(unittest.TestCase):
    def load(self, device, available=True):
        torch = SimpleNamespace(set_num_threads=MagicMock(), set_num_interop_threads=MagicMock(),
            float32='fp32', bfloat16='bf16', cuda=SimpleNamespace(is_available=lambda: available,
                get_device_name=lambda _: 'Test GPU'))
        model = MagicMock()
        modules = {'torch': torch,
            'huggingface_hub': SimpleNamespace(snapshot_download=MagicMock(return_value='cached')),
            'qwen_asr': SimpleNamespace(Qwen3ASRModel=model)}
        with patch.dict(sys.modules, modules), patch('src.ai.qwen_transcriber.config.ASR_DEVICE', device):
            QwenTranscriber()._init()
        return model.from_pretrained.call_args.kwargs

    def test_cuda_uses_bfloat16_and_retains_quality_settings(self):
        args = self.load('cuda:0')
        self.assertEqual(args['device_map'], 'cuda:0')
        self.assertEqual(args['dtype'], 'bf16')
        self.assertEqual(args['max_new_tokens'], 2048)
        self.assertEqual(args['max_inference_batch_size'], 1)

    def test_cpu_remains_float32(self):
        self.assertEqual(self.load('cpu', False)['dtype'], 'fp32')

    def test_explicit_cuda_failure_does_not_silently_use_cpu(self):
        with self.assertRaisesRegex(RuntimeError, 'CUDA requested'):
            self.load('cuda:0', False)

    def test_invalid_device_rejected(self):
        with self.assertRaises(ValueError): self.load('automatic')


if __name__ == '__main__': unittest.main()
