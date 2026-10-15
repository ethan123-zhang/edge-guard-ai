#!/bin/bash
# 冒烟测试：确认 toolkit 真的能加载模型类（不只是 import 成功）
set +e

export http_proxy="http://172.28.240.1:10090"
export https_proxy="$PROXY"

echo "=== 1. 导入检查 ==="
python3 - <<'PY'
import sys
print("python:", sys.version.split()[0])
try:
    from rkllm.api import RKLLM
    print("✓ rkllm.api.RKLLM")
except Exception as e:
    print("✗ rkllm 导入失败:", e); sys.exit(1)

try:
    import torch
    print(f"✓ torch {torch.__version__}  cuda_available={torch.cuda.is_available()}")
except Exception as e:
    print("✗ torch:", e); sys.exit(1)

try:
    import transformers
    print(f"✓ transformers {transformers.__version__}")
except Exception as e:
    print("✗ transformers:", e); sys.exit(1)

# 检查转换要用到的关键模块
for mod in ["sentencepiece", "datasets", "safetensors", "numpy"]:
    try:
        m = __import__(mod)
        print(f"✓ {mod} {getattr(m,'__version__','?')}")
    except Exception as e:
        print(f"✗ {mod}: {e}")
PY

echo
echo "=== 2. RKLLM 对象可用性 ==="
python3 - <<'PY'
from rkllm.api import RKLLM
llm = RKLLM()
print("✓ 实例化成功")
print("可用方法:", [m for m in dir(llm) if not m.startswith('_')])
PY

echo
echo "=== 3. export 示例脚本是否存在 ==="
EXPORT="$HOME/rkllm-work/rknn-llm/examples/rkllm_api_demo/export/export_rkllm.py"
if [ -f "$EXPORT" ]; then
    echo "✓ $EXPORT"
    echo "--- 脚本内容 ---"
    cat "$EXPORT"
else
    echo "✗ 没找到 export_rkllm.py，搜一下："
    find "$HOME/rkllm-work/rknn-llm/examples" -name "export_rkllm*.py" 2>/dev/null
fi

exit 0
