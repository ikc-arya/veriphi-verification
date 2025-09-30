# src/core/verification/attack_guided.py
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import torch

# --- Optional CPU memory path ---
try:
    import psutil, os  # noqa: F401
except Exception:  # pragma: no cover
    psutil = None  # type: ignore
    os = None  # type: ignore

from .base import (
    VerificationEngine,
    VerificationConfig,
    VerificationResult,
    VerificationStatus,
)
from .alpha_beta_crown import AlphaBetaCrownEngine
from ..attacks import (
    AttackConfig,
    AttackResult,
    AttackStatus,
    create_attack,
)


def _cpu_mem_mb() -> Optional[float]:
    """Return current process RSS in MB (CPU path)."""
    if psutil is None or os is None:
        return None
    try:
        return psutil.Process(os.getpid()).memory_info().rss / 1024.0 / 1024.0
    except Exception:
        return None


@dataclass
class _AttackRunRecord:
    name: str
    success: bool
    status: str
    time_s: float


class AttackGuidedEngine(VerificationEngine):
    """
    Hybrid verifier:
      1) Try fast adversarial attacks for quick falsification
      2) If no counterexample, call the formal verifier (α,β-CROWN)
    """

    def __init__(self, device: Optional[str] = None, attack_timeout: float = 10.0) -> None:
        self.device = (device or "cpu")
        self.attack_timeout = float(attack_timeout)

        # Formal engine
        self.formal_engine = AlphaBetaCrownEngine(device=self.device)

        # Instantiate default attacks (can be extended)
        self.attacks = [
            create_attack("fgsm", device=self.device),
            create_attack("i-fgsm", device=self.device),
        ]

        print(f"Initializing α,β-CROWN verifier on device: {self.device}")

    # ------------------------------------------------------------------ #
    # Public API expected by tests
    # ------------------------------------------------------------------ #
    def get_capabilities(self) -> Dict[str, Any]:
        caps = {}
        try:
            caps.update(self.formal_engine.get_capabilities())
        except Exception:
            pass

        caps["verification_strategy"] = "attack-guided"
        caps["attack_timeout"] = self.attack_timeout
        caps["attack_methods"] = ["fgsm", "i-fgsm"]
        caps["fast_falsification"] = True
        caps.setdefault("supported_norms", ["inf", "2"])
        caps.setdefault("bound_methods", ["IBP", "CROWN", "alpha-CROWN"])
        return caps

    def verify(self, model: torch.nn.Module, input_sample: torch.Tensor, config: VerificationConfig) -> VerificationResult:
        return self.verify_with_attacks(model, input_sample, config)

    # ------------------------------------------------------------------ #
    # Core flow
    # ------------------------------------------------------------------ #
    def verify_with_attacks(
        self,
        model: torch.nn.Module,
        input_sample: torch.Tensor,
        config: VerificationConfig,
    ) -> VerificationResult:
        print("🚀 Starting attack-guided verification")
        print(f"   Property: ε={config.epsilon}, norm=L{config.norm if config.norm != 'inf' else 'inf'}")
        print(f"   Attack timeout: {self.attack_timeout:.1f}s")
        print(f"   Verification timeout: {config.timeout}s")
        print(f"   🗡️ Phase 1: Attack phase (timeout: {self.attack_timeout:.1f}s)")

        attacks_tried: List[str] = []
        attack_success: Optional[AttackResult] = None

        atk_cfg = AttackConfig(
            epsilon=float(config.epsilon),
            norm=str(config.norm),
            targeted=False,
            max_iterations=5,
            early_stopping=True,
        )

        attack_t0 = time.time()
        for attack in self.attacks:
            name = attack.__class__.__name__
            attacks_tried.append(name)
            print(f"      Trying {name}...")
            res = attack.attack(model, input_sample, atk_cfg)
            if res.success:
                print("   ⚠️  Counterexample found by attack; skipping formal verification")
                attack_success = res
                break
            else:
                print(f"      ○ {name} failed to find counterexample")

        attack_time = time.time() - attack_t0
        attack_phase_result = "counterexample_found" if attack_success else "no_counterexample_found"
        print(f"   ○ Attack phase completed ({attack_time:.3f}s) - {'Counterexample found' if attack_success else 'No counterexamples found'}")

        # --- If attack succeeded ---
        if attack_success is not None:
            return self._make_falsified_result_from_attack(
                attack_success,
                attacks_tried,
                attack_time,
            )

        print("   ⚡ Attacks completed, proceeding with formal verification...")

        use_cuda = (str(self.device) == "cuda" and torch.cuda.is_available())
        if use_cuda:
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()

        t0 = time.time()
        with contextlib.ExitStack() as stack:
            if use_cuda:
                stack.enter_context(torch.cuda.amp.autocast(dtype=torch.float16))
            formal_result = self.formal_engine.verify(model, input_sample, config)

        if use_cuda:
            torch.cuda.synchronize()
        dt = time.time() - t0

        if use_cuda:
            peak_bytes = torch.cuda.max_memory_allocated()
            mem_mb = float(peak_bytes) / (1024.0 * 1024.0)
        else:
            mem_mb = _cpu_mem_mb()

        formal_result.verification_time = dt
        formal_result.memory_usage = mem_mb
        info = dict(formal_result.additional_info or {})
        info.update(
            {
                "attacks_tried": attacks_tried,
                "attack_phase_time": attack_time,
                "attack_phase_result": attack_phase_result,
                "attack_phase_completed": True,
                "phase_completed": True,
                "memory_usage_mb": mem_mb,
                "method": "attack-guided",
                "verification_method": "attack-guided",  # 🔥 added
            }
        )
        formal_result.additional_info = info
        return formal_result

    # ------------------------------------------------------------------ #
    # Batch API (added)
    # ------------------------------------------------------------------ #
    def verify_batch_with_attacks(
        self,
        model: torch.nn.Module,
        inputs: torch.Tensor,
        config: VerificationConfig,
    ) -> List[VerificationResult]:
        """Run attack-guided verification on a batch of inputs."""
        results = []
        for i in range(inputs.size(0)):
            x = inputs[i:i+1]
            res = self.verify_with_attacks(model, x, config)
            results.append(res)
        return results

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _make_falsified_result_from_attack(
        self,
        attack_res: AttackResult,
        attacks_tried: List[str],
        attack_phase_time: float,
    ) -> VerificationResult:
        result = VerificationResult(
            verified=False,
            status=VerificationStatus.FALSIFIED,
            bounds=None,
            verification_time=attack_phase_time,
            additional_info={
                "attacks_tried": attacks_tried,
                "attack_phase_time": attack_phase_time,
                "attack_phase_result": "counterexample_found",
                "attack_phase_completed": True,
                "phase_completed": True,
                "method": "attack-guided",
                "verification_method": "attack-guided",  # 🔥 added
                "falsified_by": "attack",
                "attack_name": attack_res.additional_info.get("name", "unknown") if isinstance(attack_res.additional_info, dict) else "unknown",
            },
        )
        result.memory_usage = _cpu_mem_mb()
        return result


# Factory
def create_attack_guided_engine(device: Optional[str] = None, attack_timeout: float = 10.0) -> AttackGuidedEngine:
    print("✓ Attack-guided verification engine initialized")
    return AttackGuidedEngine(device=device or "cpu", attack_timeout=attack_timeout)
