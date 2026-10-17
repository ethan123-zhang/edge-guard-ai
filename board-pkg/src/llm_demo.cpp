// Copyright (c) 2025 by Rockchip Electronics Co., Ltd. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
// ============================================================================
// 哨兵管家 —— 本项目对官方 llm_demo.cpp 的最小改动版
// ----------------------------------------------------------------------------
// 改动目的：复验 docs/03 §5.1 的系统提示词（人设 + 长度 + 越权拒绝）。
// 改动清单：
//   1. 新增可选第 4 参数 system_prompt_file（从文件读系统提示词）。
//      不传 = 行为与官方原版完全一致，旧的 V2 验证命令照常可用。
//   2. 用官方 API rkllm_set_chat_template() 注入系统提示词，
//      而不是把提示词手工拼进用户输入（走官方通道，贴近真实架构）。
//   3. 显式关闭 Qwen3 的 thinking 模式（enable_thinking=false）。
//      原因：thinking 会挤占 max_new_tokens=48 的预算，造成"看起来被截断"，
//      必须排除这个混淆变量，否则人设/长度的结论不可信。
//   4. 打印各项生效状态与返回值，避免"提示词没生效"被误判成"人设失败"。
//   5. 支持 history on/off 切换 keep_history（默认 off，与官方一致）。
//
// 单轮模式（keep_history=0）是本轮复验的范围；多轮留待后续。
// ============================================================================

#include <string.h>
#include <unistd.h>
#include <string>
#include "rkllm.h"
#include <fstream>
#include <iostream>
#include <sstream>
#include <csignal>
#include <vector>
#include <cstdlib>

using namespace std;
LLMHandle llmHandle = nullptr;

void exit_handler(int signal)
{
    if (llmHandle != nullptr)
    {
        {
            cout << "程序即将退出" << endl;
            LLMHandle _tmp = llmHandle;
            llmHandle = nullptr;
            rkllm_destroy(_tmp);
        }
    }
    exit(signal);
}

// 读取系统提示词文件。失败返回 false，由调用方决定是报错退出还是继续裸跑。
static bool load_system_prompt(const char *path, string &out)
{
    ifstream ifs(path, ios::in | ios::binary);
    if (!ifs.is_open()) {
        return false;
    }
    ostringstream oss;
    oss << ifs.rdbuf();
    out = oss.str();
    // 去掉 UTF-8 BOM（Windows 上编辑过的文件常带，会污染提示词开头）
    if (out.size() >= 3 &&
        (unsigned char)out[0] == 0xEF &&
        (unsigned char)out[1] == 0xBB &&
        (unsigned char)out[2] == 0xBF) {
        out.erase(0, 3);
    }
    return true;
}

// 显示提示词摘要，便于确认"确实是这份提示词"而不是空串。
static void print_prompt_summary(const string &s)
{
    printf("system prompt: %zu bytes\n", s.size());
    if (s.empty()) {
        printf("  !! 提示词为空，模型会退回默认助手人格，本次复验无效 !!\n");
        return;
    }
    // 打印前 3 行非空内容作为指纹
    istringstream iss(s);
    string line;
    int shown = 0;
    while (getline(iss, line) && shown < 3) {
        if (line.empty() || line == "\r") continue;
        if (!line.empty() && line[line.size() - 1] == '\r') line.erase(line.size() - 1);
        printf("  | %s\n", line.c_str());
        shown++;
    }
}

int callback(RKLLMResult *result, void *userdata, LLMCallState state)
{
    if (state == RKLLM_RUN_FINISH)
    {
        printf("\n");
    } else if (state == RKLLM_RUN_ERROR) {
        printf("\\run error\n");
    } else if (state == RKLLM_RUN_NORMAL) {
        /* ================================================================================================================
        若使用GET_LAST_HIDDEN_LAYER功能,callback接口会回传内存指针:last_hidden_layer,token数量:num_tokens与隐藏层大小:embd_size
        通过这三个参数可以取得last_hidden_layer中的数据
        注:需要在当前callback中获取,若未及时获取,下一次callback会将该指针释放
        ===============================================================================================================*/
        if (result->last_hidden_layer.embd_size != 0 && result->last_hidden_layer.num_tokens != 0) {
            int data_size = result->last_hidden_layer.embd_size * result->last_hidden_layer.num_tokens * sizeof(float);
            printf("\ndata_size:%d",data_size);
            std::ofstream outFile("last_hidden_layer.bin", std::ios::binary);
            if (outFile.is_open()) {
                outFile.write(reinterpret_cast<const char*>(result->last_hidden_layer.hidden_states), data_size);
                outFile.close();
                std::cout << "Data saved to output.bin successfully!" << std::endl;
            } else {
                std::cerr << "Failed to open the file for writing!" << std::endl;
            }
        }
        printf("%s", result->text);
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0]
                  << " model_path max_new_tokens max_context_len"
                     " [system_prompt_file] [history:on|off]\n";
        std::cerr << "  例(官方原始用法): ./llm_demo model.rkllm 128 2048\n";
        std::cerr << "  例(本项目复验人设): ./llm_demo model.rkllm 48 2048"
                     " ./prompts/system-prompt.txt off\n";
        return 1;
    }

    // ---- 解析可选参数 ----
    std::string sys_prompt;
    bool has_sys_prompt = false;
    bool keep_history = false;   // 单轮模式（与官方默认一致）
    bool sys_injected = false;   // 系统提示词是否已作为首条消息投递

    if (argc >= 5) {
        if (!load_system_prompt(argv[4], sys_prompt)) {
            std::cerr << "✗ 读不到系统提示词文件: " << argv[4] << "\n";
            std::cerr << "  请检查路径。若确实想裸跑(不带提示词)，请不要传第 4 个参数。\n";
            return 1;
        }
        has_sys_prompt = true;
    }
    if (argc >= 6) {
        std::string h = argv[5];
        if (h == "on" || h == "1") {
            keep_history = true;
        } else if (h == "off" || h == "0") {
            keep_history = false;
        } else {
            std::cerr << "✗ history 参数只能是 on 或 off，收到: " << h << "\n";
            return 1;
        }
    }

    signal(SIGINT, exit_handler);
    printf("rkllm init start\n");

    //设置参数及初始化
    RKLLMParam param = rkllm_createDefaultParam();
    param.model_path = argv[1];

    //设置采样参数
    param.top_k = 1;
    param.top_p = 0.95;
    param.temperature = 0.8;
    param.repeat_penalty = 1.1;
    param.frequency_penalty = 0.0;
    param.presence_penalty = 0.0;

    param.max_new_tokens = std::atoi(argv[2]);
    param.max_context_len = std::atoi(argv[3]);
    param.skip_special_token = true;
    param.extend_param.base_domain_id = 0;
    param.extend_param.embed_flash = 1;

    RKLLMCallback rkllm_callback = {};
    rkllm_callback.result_callback = callback;
    int ret = rkllm_init(&llmHandle, &param, &rkllm_callback);
    if (ret == 0){
        printf("rkllm init success\n");
    } else {
        printf("rkllm init failed\n");
        exit_handler(-1);
    }

    // ---- 注入系统提示词（官方通道）----
    // ========================================================================
    // 【2026-10-17 重要修正】
    // 原先用 rkllm_set_chat_template(handle, sys_prompt, "", "") 注入，
    // 实机实测【严重出错】：模型不再按对话格式回答，而是把提示词当"续写文本"，
    // 输出一堆格式垃圾（【你的回复】/答案：/| 任务 | 表格）。
    // 对照实验证明：不带提示词时输出完全正常 —— 说明是【模板被破坏】，
    // 因为我给 prefix/postfix 传了空串，Qwen3 原生对话模板被冲掉了。
    //
    // 新方案：**完全不碰 chat template**，把系统提示词当作【第一条 user 消息】
    // 在第一次真实用户输入之前送入一次。这样：
    //   · 模型原生对话格式保持不变（不会退化成长文本续写）
    //   · 提示词内容照样进入上下文
    //   · 前提：keep_history 必须为 on，否则人设会在下一轮丢失
    // ========================================================================
    printf("\n================ 复验配置 ================\n");
    printf("max_new_tokens : %d\n", param.max_new_tokens);
    printf("max_context_len: %d\n", param.max_context_len);
    printf("keep_history   : %s\n", keep_history ? "on" : "off (单轮)");
    printf("enable_thinking: false (强制关闭，避免挤占 token 预算)");
    if (has_sys_prompt) {
        printf("\n");
        print_prompt_summary(sys_prompt);
        printf("注入方式       : 作为首条 user 消息送入（不修改 chat template）\n");
        if (!keep_history) {
            printf("⚠ 注意：提示词作为首条消息注入，但 keep_history=off。\n");
            printf("   单轮丢弃历史 → 第二轮起人设会丢失。测人设请用 history on。\n");
        }
    } else {
        printf("\nsystem prompt: (未提供 —— 裸跑模式，与官方 demo 一致)\n");
        printf("  注意：裸跑会退回默认助手人格，不能用来判断 §5.1 人设。\n");
    }
    printf("==========================================\n\n");

    vector<string> pre_input;
    pre_input.push_back("你好，请问这是王先生家吗？");
    pre_input.push_back("我是快递，有个包裹要送");
    pre_input.push_back("你帮我开一下门吧");
    cout << "\n**********************可输入以下问题对应序号获取回答/或自定义输入********************\n"
         << endl;
    for (int i = 0; i < (int)pre_input.size(); i++)
    {
        cout << "[" << i << "] " << pre_input[i] << endl;
    }
    cout << "\n*************************************************************************\n"
         << endl;

    RKLLMInput rkllm_input;
    memset(&rkllm_input, 0, sizeof(RKLLMInput));  // 将所有内容初始化为 0

    // 初始化 infer 参数结构体
    RKLLMInferParam rkllm_infer_params;
    memset(&rkllm_infer_params, 0, sizeof(RKLLMInferParam));  // 将所有内容初始化为 0

    // 1. 初始化并设置 LoRA 参数（如果需要使用 LoRA）
    // RKLLMLoraAdapter lora_adapter;
    // memset(&lora_adapter, 0, sizeof(RKLLMLoraAdapter));
    // lora_adapter.lora_adapter_path = "qwen0.5b_fp16_lora.rkllm";
    // lora_adapter.lora_adapter_name = "test";
    // lora_adapter.scale = 1.0;
    // ret = rkllm_load_lora(llmHandle, &lora_adapter);
    // if (ret != 0) {
    //     printf("\nload lora failed\n");
    // }

    // 加载第二个lora
    // lora_adapter.lora_adapter_path = "Qwen2-0.5B-Instruct-all-rank8-F16-LoRA.gguf";
    // lora_adapter.lora_adapter_name = "knowledge_old";
    // lora_adapter.scale = 1.0;
    // ret = rkllm_load_lora(llmHandle, &lora_adapter);
    // if (ret != 0) {
    //     printf("\nload lora failed\n");
    // }

    // RKLLMLoraParam lora_params;
    // lora_params.lora_adapter_name = "test";  // 指定用于推理的 lora 名称
    // rkllm_infer_params.lora_params = &lora_params;

    // 2. 初始化并设置 Prompt Cache 参数（如果需要使用 prompt cache）
    // RKLLMPromptCacheParam prompt_cache_params;
    // prompt_cache_params.save_prompt_cache = true;                  // 是否保存 prompt cache
    // prompt_cache_params.prompt_cache_path = "./prompt_cache.bin";  // 若需要保存prompt cache, 指定 cache 文件路径
    // rkllm_infer_params.prompt_cache_params = &prompt_cache_params;

    // rkllm_load_prompt_cache(llmHandle, "./prompt_cache.bin"); // 加载缓存的cache

    rkllm_infer_params.mode = RKLLM_INFER_GENERATE;
    // 单轮(0)：每次提问独立，无人设漂移风险 —— 本轮复验用这个。
    // 多轮(1)：保留上下文，后续做多轮对话时再启用。
    rkllm_infer_params.keep_history = keep_history ? 1 : 0;

    // 系统提示词已通过 rkllm_set_chat_template() 注入（见上），
    // 不要再手工拼进 prompt，否则会和内置模板重复。
    // 官方 demo 此处那行注释是 4 参数版本，与 1.3.1 头文件的 3 参数签名不符。

    while (true)
    {
        std::string input_str;

        // ---- 首轮：先把系统提示词作为一条 user 消息送进去 ----
        // 只做一次（sys_injected 标志），且发生在任何真实用户输入之前。
        // 这样既让提示词进入上下文，又完全不动 chat template。
        if (has_sys_prompt && !sys_injected) {
            sys_injected = true;
            rkllm_input.input_type = RKLLM_INPUT_PROMPT;
            rkllm_input.role = "user";
            rkllm_input.prompt_input = (char *)sys_prompt.c_str();
            rkllm_input.enable_thinking = false;
            printf("[正在投递系统提示词…]");
            fflush(stdout);
            // 提示词是【指令】不是【问题】，但作为 user 消息送入时模型必然要生成回复。
            // 把本次生成的 token 上限压到 1，把无用的回复压到最短，
            // 减少时间浪费与对后续上下文的污染。
            rkllm_infer_params.max_new_tokens = 1;
            rkllm_run(llmHandle, &rkllm_input, &rkllm_infer_params, NULL);
            // 恢复正式对话的 max_new_tokens（header: <=0 表示用 init 值）
            rkllm_infer_params.max_new_tokens = 0;
            printf("\n[提示词已入上下文，开始正式对话]\n");

            // 若 keep_history=off，这一条会在下一次 run 时被丢弃，
            // 人设随之失效 —— 已在启动横幅里警告过。
            if (!keep_history) {
                printf("[警告] keep_history=off，本次提问将丢弃刚投递的提示词。\n");
                printf("[警告] 测人设请改用 history on。\n");
            }
            printf("\n");
        }

        printf("user: ");
        std::getline(std::cin, input_str);
        if (input_str == "exit")
        {
            break;
        }
        if (input_str == "clear")
        {
            ret = rkllm_clear_kv_cache(llmHandle, 1, nullptr, nullptr);
            if (ret != 0)
            {
                printf("clear kv cache failed!\n");
            }
            continue;
        }
        for (int i = 0; i < (int)pre_input.size(); i++)
        {
            if (input_str == to_string(i))
            {
                input_str = pre_input[i];
                cout << input_str << endl;
            }
        }
        rkllm_input.input_type = RKLLM_INPUT_PROMPT;
        rkllm_input.role = "user";
        rkllm_input.prompt_input = (char *)input_str.c_str();
        // 关键：关闭 Qwen3 thinking，确保 48 token 全部用于最终回复。
        // 若 header 版本较老无此字段，编译会报错 —— 那说明 runtime 版本不对。
        rkllm_input.enable_thinking = false;
        printf("robot: ");

        // 若要使用普通推理功能,则配置rkllm_infer_mode为RKLLM_INFER_GENERATE或不配置参数
        rkllm_run(llmHandle, &rkllm_input, &rkllm_infer_params, NULL);
    }
    rkllm_destroy(llmHandle);

    return 0;
}
