// 仅用于本机编译检查的桩实现（不下板、不链接真的 librkllmrt.so）
// 目的：验证 llm_demo.cpp 与官方 rkllm.h 的类型/签名一致。
#include "rkllm.h"
#include <cstring>
#include <cstdio>

extern "C" {

RKLLMParam rkllm_createDefaultParam(void) {
    RKLLMParam p;
    memset(&p, 0, sizeof(p));
    return p;
}

int rkllm_init(LLMHandle *handle, RKLLMParam *param, RKLLMCallback *callback) {
    (void)param; (void)callback;
    static int dummy = 0;
    *handle = (LLMHandle)&dummy;
    return 0;
}

int rkllm_run(LLMHandle handle, RKLLMInput *input, RKLLMInferParam *params, void *userdata) {
    (void)handle; (void)params; (void)userdata;
    printf("[stub] got prompt: %s\n", input->prompt_input);
    printf("[stub] enable_thinking=%d\n", (int)input->enable_thinking);
    return 0;
}

int rkllm_set_chat_template(LLMHandle handle, const char *system_prompt,
                            const char *prompt_prefix, const char *prompt_postfix) {
    (void)handle; (void)prompt_prefix; (void)prompt_postfix;
    printf("[stub] set_chat_template system_prompt=%zu bytes\n",
           system_prompt ? strlen(system_prompt) : (size_t)0);
    return 0;
}

int rkllm_clear_kv_cache(LLMHandle handle, int keep_system_prompt,
                         int *start_pos, int *end_pos) {
    (void)handle; (void)keep_system_prompt; (void)start_pos; (void)end_pos;
    return 0;
}

int rkllm_destroy(LLMHandle handle) { (void)handle; return 0; }

} // extern "C"
