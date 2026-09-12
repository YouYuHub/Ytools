# -*- coding: utf-8 -*-
"""
猜数字小游戏
================
玩法：
1. 程序在 1~100（或按难度调整范围）内随机生成一个整数
2. 玩家输入猜测的数字，程序提示"大了 / 小了"
3. 猜中后显示所用次数，并提供再来一局

运行：python guess_number.py
"""
import random
import sys


def color(text, code):
    """终端彩色输出（Windows 兼容）"""
    return f"\033[{code}m{text}\033[0m"


def select_difficulty():
    """选择难度，返回 (最小数, 最大数, 允许次数)"""
    print(color("\n========== 选择难度 ==========", "36"))
    print(" 1. 简单  1~50    最多 10 次")
    print(" 2. 普通  1~100   最多 8 次")
    print(" 3. 困难  1~200   最多 7 次")
    print(" 4. 地狱  1~1000  最多 10 次")
    while True:
        choice = input("请输入难度编号 (1-4)：").strip()
        table = {
            "1": (1, 50, 10, "简单"),
            "2": (1, 100, 8, "普通"),
            "3": (1, 200, 7, "困难"),
            "4": (1, 1000, 10, "地狱"),
        }
        if choice in table:
            low, high, max_try, name = table[choice]
            print(color(f"已选择【{name}】难度：范围 {low}~{high}，最多 {max_try} 次", "33"))
            return low, high, max_try, name
        print(color("输入无效，请输入 1-4 的数字！", "31"))


def play_one_round(stats):
    """进行一局游戏"""
    low, high, max_try, diff_name = select_difficulty()
    secret = random.randint(low, high)
    print(color(f"\n>>> 我已想好一个 {low}~{high} 之间的整数，开始猜吧！", "32"))

    used = 0
    while True:
        used += 1
        if used > max_try:
            print(color(f"很遗憾，{max_try} 次机会已用完。正确答案是 {secret}，下次加油！", "31"))
            stats["losses"] += 1
            return used

        raw = input(f"[第 {used}/{max_try} 次] 请输入你的猜测：").strip()
        if raw.lower() in ("q", "quit", "exit"):
            print("中途退出，再见！")
            sys.exit(0)

        try:
            guess = int(raw)
        except ValueError:
            print(color("请输入一个整数！", "31"))
            used -= 1  # 无效输入不计数
            continue

        if guess < low or guess > high:
            print(color(f"超出范围！请输入 {low}~{high} 之间的整数。", "31"))
            used -= 1
            continue

        if guess > secret:
            print(color(f"大了！答案在 {low}~{guess - 1} 之间。", "33"))
            high = guess - 1
        elif guess < secret:
            print(color(f"小了！答案在 {guess + 1}~{high} 之间。", "33"))
            low = guess + 1
        else:
            # 猜中了，按次数评定星级
            if used == 1:
                star = "★★★★★ 一击必中，天选之人！"
            elif used <= 3:
                star = "★★★★☆ 神速！"
            elif used <= max_try // 2:
                star = "★★★☆☆ 不错！"
            elif used <= max_try:
                star = "★★☆☆☆ 刚好过关～"
            print(color(f"\n🎉 恭喜猜中！答案就是 {secret}，你用了 {used} 次。{star}", "32"))
            stats["wins"] += 1
            stats["total_used"] += used
            return used


def main():
    print(color("=" * 46, "36"))
    print(color("        🎮 猜数字小游戏  Guess Number", "36"))
    print(color("=" * 46, "36"))
    print("输入数字进行猜测，输入 q 可随时退出游戏。")

    stats = {"wins": 0, "losses": 0, "total_used": 0}
    total_games = 0

    while True:
        total_games += 1
        used = play_one_round(stats)

        # 战绩统计
        print(color("-" * 46, "36"))
        print(f"当前战绩：共 {total_games} 局 | 胜利 {stats['wins']} | 失败 {stats['losses']}"
              + (f" | 平均 {stats['total_used'] / stats['wins']:.1f} 次/胜" if stats["wins"] else ""))
        print(color("-" * 46, "36"))

        again = input("再来一局？(y/n)：").strip().lower()
        if again not in ("y", "yes", ""):
            print(color("\n感谢游玩，再见！👋", "36"))
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n收到中断信号，再见！")
        sys.exit(0)
