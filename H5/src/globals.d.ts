/**
 * 跨文件共享全局的类型声明（仅供 tsc 检查，不发射代码）。
 *
 * 背景：前端是 IIFE + window 全局 + <script> 顺序加载，各模块经裸标识符
 * （API/AccountPreferences/…）或 window.X 互相引用。TS 源码里：
 * - 读裸全局：靠本文件的 `declare const X: any` 通过检查；
 * - 写 window.X：各模块自行 `(window as unknown as { X: … }).X = …`，
 *   走属性赋值，不与本文件的 const 声明冲突；
 * - window.X 读取：各模块自行断言（Window 接口没有这些属性）。
 *
 * 每迁移一个引用新裸全局的模块，就在本文件加一行。
 */
declare const API: any;
declare const AccountPreferences: any;
declare const TaskState: any;
declare const HistorySearch: any;
declare const ToolApprovalState: any;
declare const FormatUtils: any;
declare const HistoryParser: any;
declare const HistoryParseAsync: any;
declare const YtoolsAuth: any;
declare const SessionUtils: any;
declare const ThemeManager: any;
declare const ClipboardMediaUtils: any;
declare const Markdown: any;
declare const SessionGroupUtils: any;
declare const StreamReplayDedup: any;
declare const QuoteUtils: any;
declare const Prism: any;
declare const SessionListUtils: any;
declare const WidgetReuse: any;
declare const ReadMediaStrip: any;
declare const TableCanvas: any;
