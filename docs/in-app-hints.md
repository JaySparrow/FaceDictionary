# 界面操作提示（已从应用中移除，留作日后教程素材）

2026-11-13 起，应用界面不再显示操作说明。下面记录原来的提示文字和位置，以后做应用内教程时可直接取用。

## 搜索页

| 位置 | 原提示 |
|---|---|
| 空的搜索框（虚线框）中央 | Drop a photo here / or click to choose one / ⌘V paste · ⌘O open |
| 搜索框下方状态行 | Drop a photo, press ⌘V to paste one, or ⌘O to open one. |
| 多张脸时的说明行 | Click another face (or use ← →) to see its matches. |
| 悬停在人脸框上的提示 | Face {n} – click to see its matches |
| 找不到人脸时 | Try a clearer, more front-facing photo. |
| 右侧空的结果区 | Results appear here. |

## 词条页（Library）

| 位置 | 原提示 |
|---|---|
| 未选词条时右侧中央 | Select an entry, create a new one, or import a folder. |
| 悬停在可编辑文字上 | 浅色底纹 + “Double-click to edit”（空的项为 “Click to add”） |
| 名字 | Double-click to rename |
| 字段名 | Shared by every entry of this database / Only in this entry – double-click to rename |
| 别名、标签为空时 | add aliases / add tags（现为灰色的 Aliases / Tags） |
| 别名、标签输入框 | Other names, separated by commas / Tags, separated by commas |
| 多行字段输入框 | ⌘↩ to save, Esc to cancel |
| 字段行末悬停出现的 “–” | Remove “{name}”（现只在右键菜单，菜单项写明字段名） |
| 共享与个人字段之间的分隔线 | only in this entry（现为一根无标题的线，没有个人字段时不显示） |
| 字段区末尾的两个链接 | + Add a field to all entries / + Add a field to this entry（现在右键菜单和 Edit 菜单里） |
| Photos 标题行 | Drag between photos to move, onto a photo to swap · the first one is the cover |
| 缩略图下方 | cover / N faces / from a search（现在封面用细框表示，其余不写） |
| 照片缩略图悬停 | Double-click to see it big; drag to move it. / double-click to check which one is used. |
| 删除确认框 | ⌘Z puts it back. / ⌘Z or the Trash bring it back. |

## 照片窗口

| 位置 | 原提示 |
|---|---|
| 说明行 | Double-click a face to use it for matching. |
| 悬停在人脸框上 | Face {n} – double-click to use it for matching |
| 人脸缩略图悬停 | Face {n} – double-click to use it |
| 底部状态 | Added {时间}, imported. Face {n} (green) is used for matching. 及其后的 double-click 提示（整行已移除） |
| 说明行（人脸数） | {n} faces found – face {i} is selected.（已隐藏） |

## 窗口底部状态栏

| 原内容 | 现在 |
|---|---|
| 数据库名 · 词条数 · 照片数 · 已索引人脸数 · 搜索次数 · 模型名称 | 不显示；只在准备索引或有文件读不出时显示 |

## 保留的文字

- 符号按钮的悬停名称（如 + 的 “Add photos to this entry”），因为按钮上只有符号。
- 操作后果的警告：导出不加密、删除进回收站、删除共享字段影响所有词条、导入文件夹的结构要求。
- 解锁页和设置里关于密码、密保问题的说明。
- 状态信息：人脸数量、用了哪张脸、检索耗时、照片的添加时间和来源。

## 操作速查（教程要点）

- 文字：双击编辑，空的项单击即可填写。回车、Tab 或点别处保存，多行字段用 ⌘回车，Esc 放弃。
- 字段：右键字段区或 Edit 菜单：Add Field to All Entries（⇧⌘;）、Add Field to This Entry（⌘;）。右键某个字段：Copy / Edit… / Remove “字段名”（菜单打开时该行有浅色底）。双击字段名改名，清空字段名即删除该字段。
- 照片：+、⌘O 或把照片拖进相册来添加，– 移到回收站，🚫 不用于匹配。拖到两张之间是插入，拖到某张上是交换，第一张（带框）是封面。
- 照片窗口：双击人脸框或缩略图改用那张脸匹配，单击不改变任何东西。下方的 Note 可写这张照片的注释。
- 撤销：⌘Z 连续撤销，⇧⌘Z 重做（Edit 菜单）。正在输入时 ⌘Z 撤销的是打字。
- 删除：⌫（或 ⌦）删除当前列表里选中的那一项，和对应的删除按钮一样会先确认：词条列表删词条，相册删照片，历史删记录，回收站永久删除（问两次）。字段没有选中状态，用清空字段名删除。输入文字时 ⌫ 仍是退格。
- 搜索：拖入照片、点击空白搜索框或标题栏的 +、⌘V 粘贴或 ⌘O 打开。点人脸框、缩略图，或用 ←/→、1–9 切换人脸。
