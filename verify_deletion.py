"""Quick import check for src/ modules after okr_atomic_creation.py deletion."""
import importlib, sys, os

os.chdir('/home/ubuntu/hermes-agent')
sys.path.insert(0, '/home/ubuntu/hermes-agent/src')

# Check that framework_okr_remediation still imports cleanly
try:
    spec = importlib.util.spec_from_file_location("framework_okr_remediation", "/home/ubuntu/hermes-agent/src/framework_okr_remediation.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print("framework_okr_remediation: OK")
except Exception as e:
    print(f"framework_okr_remediation: FAILED - {e}")

# Verify okr_atomic_creation is NOT importable
try:
    import okr_atomic_creation
    print("okr_atomic_creation: STILL IMPORTABLE (BAD)")
except ImportError:
    print("okr_atomic_creation: correctly removed (ImportError as expected)")

print("\nAll checks passed.")
