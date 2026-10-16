// 测试驱动：调用被改名的 llm_demo main（argv 由本文件构造）
// 需要给 main 喂入 "exit" 才能退出循环，所以这里先重定向 stdin 到 EOF。
#include <cstdio>
#include <string>
#include <vector>
#include <unistd.h>

int llm_demo_main(int argc, char **argv);

// 用临时文件冒充 stdin，内容为 "exit"，让交互循环立刻结束
static void feed_stdin_exit(const char *path) {
    FILE *f = fopen(path, "w");
    if (f) { fputs("exit\n", f); fclose(f); }
    freopen(path, "r", stdin);
}

int main(int argc, char **argv) {
    const char *tmp = "stdin_exit.txt";
    feed_stdin_exit(tmp);

    // 把测试驱动的参数（去掉 argv[0]）转交给 llm_demo_main
    std::vector<std::string> store;
    for (int i = 1; i < argc; i++) store.push_back(argv[i]);
    std::vector<char *> a;
    a.push_back((char *)"llm_demo");
    for (size_t i = 0; i < store.size(); i++) a.push_back((char *)store[i].c_str());

    return llm_demo_main((int)a.size(), a.data());
}
