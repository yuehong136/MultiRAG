# coding=utf-8
"""
@project: multirag
@Author：龙
@file： init_sensitive_words.py
@date：2025/07/09 10:30
@desc: 初始化敏感词库数据 - 基于内容安全检测标准(7大类)和六地AI合规要求

功能概述:
1. 内容安全检测标准 - 涵盖7大类检测:
   - 色情内容检测: pornographic_adult, sexual_terms, sexual_suggestive, sexual_prompts
   - 涉政内容检测: political_figure, political_entity, political_n, political_p, political_prompts, political_a
   - 暴恐内容检测: violent_extremists, violent_incidents, violent_weapons, violent_prompts
   - 违禁内容检测: contraband_drug, contraband_gambling, contraband_act, contraband_entity
   - 不良内容检测: inappropriate_minor, inappropriate_discrimination, inappropriate_ethics, inappropriate_profanity, inappropriate_oral, inappropriate_superstition, inappropriate_nonsense
   - 宗教内容检测: religion_b, religion_t, religion_c, religion_i, religion_h
   - 广告内容检测: pt_to_sites, pt_by_recruitment, pt_to_contact
   - 隐私敏感检测: privacy_p, privacy_b

2. 国际AI合规要求 - 六地法规覆盖:
   - 中国 TC260-003 生成式AI服务安全要求
   - 欧盟 AI法案 (Reg. EU 2024/1689)
   - 美国 NIST AI 100-2e 安全标准
   - 香港 AI道德标准指引
   - 马来西亚 AI治理框架
   - 印尼 电子信息和交易法修订

3. 风险分级体系:
   - 监控级 (0-49分): 仅记录，不拦截
   - 警告级 (50-69分): 警告提示
   - 中风险 (70-84分): 替换或部分拦截
   - 高风险 (85-94分): 直接拦截
   - 极高风险 (95-100分): 拦截并上报
   - 法律/合规风险: 最高级别拦截
   - 已关闭检测: 检测状态已关闭
"""

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from api.db.db_models import SessionLocal
from api.db.services.sensitive_word_service import (
    SensitiveWordCategoryService,
    SensitiveWordLevelService, 
    SensitiveWordService,
    SensitiveWordWhitelistService
)
from api.utils import get_uuid


def init_default_categories(db, tenant_id: str, user_id: str):
    """初始化默认分类 - 基于内容安全检测标准"""
    categories = [
        # 1. 色情内容检测
        {"name": "色情成人内容", "description": "性器官描述、性行为内容、色情交易服务等直白描述"},
        {"name": "性健康内容", "description": "性知识科普、性教育、性健康等内容"},
        {"name": "低俗暗示内容", "description": "低俗擦边、软色情、性暗示、敏感部位描述等"},
        {"name": "色情诱导提示", "description": "诱导生成色情内容的提示词描述"},
        
        # 2. 涉政内容检测
        {"name": "政治人物", "description": "国家核心领导人、负面代称、英雄烈士相关"},
        {"name": "政治实体", "description": "政治事件活动、军事言论、机关单位名称等"},
        {"name": "敏感政治内容", "description": "不宜传播的政治历史事件、分裂国家言论等"},
        {"name": "涉政禁宣人物", "description": "战犯、落马官员、劣迹艺人等不宜宣传人物"},
        {"name": "涉政诱导提示", "description": "诱导生成涉政内容的提示词描述"},
        {"name": "涉政专项保障", "description": "特定时间节点的涉政专项保障内容"},
        
        # 3. 暴恐内容检测
        {"name": "极端组织", "description": "境内外暴力恐怖/极端主义个人或组织"},
        {"name": "极端主义内容", "description": "宣扬传播极端恐怖主义的行为或言论"},
        {"name": "武器弹药", "description": "核生化武器、常规战斗型武器、刀具等"},
        {"name": "暴力诱导提示", "description": "诱导生成暴力血腥内容的提示词描述"},
        
        # 4. 违禁内容检测
        {"name": "毒品相关", "description": "毒品、精神药物、致幻类药品"},
        {"name": "赌博相关", "description": "赌博行为、工具、涉赌游戏等"},
        {"name": "违禁行为", "description": "违法犯罪事件、制假售假、信用卡套现等"},
        {"name": "违禁工具", "description": "色情域名APP、翻墙软件工具教程等"},
        
        # 5. 不良内容检测
        {"name": "未成年不适内容", "description": "侵犯未成年身心健康、涉及未成年不良行为"},
        {"name": "偏见歧视内容", "description": "国别地域种族民族信仰性别职业等偏见歧视"},
        {"name": "不良价值观内容", "description": "违反主流道德观念的言论或行为"},
        {"name": "攻击辱骂内容", "description": "人身攻击诅咒、辱骂诽谤、挑起争端等"},
        {"name": "低俗口头语内容", "description": "辱骂口头语、粗口等低俗用语"},
        {"name": "封建迷信内容", "description": "宣传封建迷信的言论或行为"},
        {"name": "无意义灌水内容", "description": "非中文灌水、特殊符号占位、重复内容等"},
        
        # 6. 宗教内容检测
        {"name": "佛教相关", "description": "佛教教规、人物、经文、活动、教义、知识等"},
        {"name": "道教相关", "description": "道教教规、人物、经文、活动、教义、知识等"},
        {"name": "基督教相关", "description": "基督教教规、人物、经文、活动、教义、知识等"},
        {"name": "伊斯兰教相关", "description": "伊斯兰教教规、人物、经文、活动、教义、知识等"},
        {"name": "印度教相关", "description": "印度教教规、人物、经文、活动、教义、知识等"},
        
        # 7. 广告内容检测
        {"name": "站外引流", "description": "引流至社交游戏短视频等其他站外平台"},
        {"name": "网赚兼职广告", "description": "兼职招聘、网赚导流等广告"},
        {"name": "引流广告号", "description": "账号群号引流、电话号引流、网址邮箱等"},
        
        # 8. 隐私敏感检测
        {"name": "个人隐私信息", "description": "诱导泄露个人账密、家庭住址、身份证号等"},
        {"name": "商业敏感内容", "description": "诱导泄露商业版权、商业秘密等敏感数据"},
        
        # 9. AI安全防护 (基于NIST等标准)
        {"name": "提示注入攻击", "description": "直接和间接提示攻击、越狱指令"},
        {"name": "模型操纵", "description": "试图操纵模型行为的恶意输入"},
        {"name": "信息提取攻击", "description": "试图提取训练数据或系统信息的攻击"},
        
        # 10. 其他
        {"name": "其他敏感", "description": "其他类型敏感词汇"}
    ]
    
    created_categories = {}
    for cat_data in categories:
        category_id = get_uuid()
        category_info = {
            "id": category_id,
            "name": cat_data["name"],
            "description": cat_data["description"],
            "tenant_id": tenant_id,
            "created_by": user_id
        }
        
        if SensitiveWordCategoryService.save(db, **category_info):
            created_categories[cat_data["name"]] = category_id
            print(f"✓ 创建分类: {cat_data['name']}")
        else:
            print(f"✗ 创建分类失败: {cat_data['name']}")
    
    return created_categories


def init_default_levels(db, tenant_id: str):
    """初始化默认等级 - 基于内容安全风险分级标准"""
    levels = [
        {
            "name": "监控级", 
            "level": 1, 
            "description": "低风险(0-49分)，仅记录监控，不做拦截", 
            "action": "monitor", 
            "replacement": None
        },
        {
            "name": "警告级", 
            "level": 2, 
            "description": "轻微风险(50-69分)，发出警告提示", 
            "action": "warn", 
            "replacement": None
        },
        {
            "name": "中风险替换级", 
            "level": 3, 
            "description": "中风险(70-74分)，进行内容替换", 
            "action": "replace", 
            "replacement": "***"
        },
        {
            "name": "中风险拦截级", 
            "level": 4, 
            "description": "中风险(75-84分)，部分拦截或强制替换", 
            "action": "partial_block", 
            "replacement": "[敏感内容]"
        },
        {
            "name": "高风险拦截级", 
            "level": 5, 
            "description": "高风险(85-94分)，直接拦截请求", 
            "action": "block", 
            "replacement": None
        },
        {
            "name": "极高风险级", 
            "level": 6, 
            "description": "极高风险(95-100分)，拦截并上报", 
            "action": "block_and_report", 
            "replacement": None
        },
        {
            "name": "法律风险级", 
            "level": 7, 
            "description": "涉及法律风险，立即拦截并记录", 
            "action": "legal_block", 
            "replacement": None
        },
        {
            "name": "合规违规级", 
            "level": 8, 
            "description": "违反合规要求，最高级别拦截", 
            "action": "compliance_block", 
            "replacement": None
        },
        {
            "name": "已关闭检测", 
            "level": 0, 
            "description": "检测状态为已关闭的内容类别", 
            "action": "disabled", 
            "replacement": None
        }
    ]
    
    created_levels = {}
    for level_data in levels:
        level_id = get_uuid()
        level_info = {
            "id": level_id,
            "name": level_data["name"],
            "level": level_data["level"],
            "description": level_data["description"],
            "action": level_data["action"],
            "replacement": level_data["replacement"],
            "tenant_id": tenant_id
        }
        
        if SensitiveWordLevelService.save(db, **level_info):
            created_levels[level_data["name"]] = level_id
            print(f"✓ 创建等级: {level_data['name']} (Level {level_data['level']})")
        else:
            print(f"✗ 创建等级失败: {level_data['name']}")
    
    return created_levels


def init_sample_sensitive_words(db, categories: dict, levels: dict, tenant_id: str, user_id: str):
    """初始化示例敏感词 - 基于内容安全检测标准"""
    
    # 示例敏感词数据 - 根据7大类内容安全检测标准分类
    sample_words = {
        # 中国TC260-003要求
        "政治敏感": {
            "level": "合规违规级",
            "words": ["反动分子", "政治颠覆", "非法集会", "政权更迭", "分裂国家"],
            "description": "符合中国TC260-003模型安全要求"
        },
        "暴力恐怖": {
            "level": "法律风险级",
            "words": ["恐怖袭击", "制造炸弹", "暴力推翻", "恐怖主义", "极端组织"],
            "description": "反恐法规要求"
        },
        "违法犯罪": {
            "level": "法律风险级",
            "words": ["贩毒指南", "洗钱教程", "非法武器", "人口贩卖", "器官买卖"],
            "description": "刑事法律风险"
        },
        
        # 欧盟AI法案Art.5禁止用途
        "生物识别滥用": {
            "level": "合规违规级",
            "words": ["无理由人脸识别", "情绪识别筛选", "行为预测分析", "生物特征追踪"],
            "description": "欧盟AI法案第5条禁止用途"
        },
        "社会评分": {
            "level": "合规违规级", 
            "words": ["社会信用评分", "行为打分系统", "道德评价算法", "社会排名"],
            "description": "欧盟AI法案禁止的社会评分系统"
        },
        "弱势群体歧视": {
            "level": "高风险拦截级",
            "words": ["种族优劣论", "性别歧视", "宗教仇恨", "残疾人歧视", "年龄歧视"],
            "description": "欧盟反歧视法规"
        },
        "儿童伤害": {
            "level": "合规违规级",
            "words": ["儿童不当内容", "未成年人剥削", "校园暴力", "儿童心理伤害"],
            "description": "儿童保护法规"
        },
        
        # 美国NIST AI 100-2e攻击防护
        "提示注入攻击": {
            "level": "高风险拦截级",
            "words": ["system prompt", "jailbreak", "prompt injection", "model extraction"],
            "description": "NIST 3.3直接提示攻击"
        },
        "模型操纵": {
            "level": "高风险拦截级",
            "words": ["模型权重提取", "训练数据逆向", "梯度攻击", "模型后门", "对抗样本"],
            "description": "模型安全攻击"
        },
        "信息提取攻击": {
            "level": "高风险拦截级",
            "words": ["训练数据泄露", "系统信息获取", "内部API调用", "配置文件读取"],
            "description": "NIST信息提取攻击防护"
        },
        
        # 香港PDPO数据隐私要求
        "个人隐私": {
            "level": "拦截级",
            "words": ["身份证号码", "银行卡信息", "医疗记录", "个人地址", "私人通信"],
            "description": "香港个人资料私隐条例"
        },
        "数据泄露": {
            "level": "法律风险级",
            "words": ["数据库泄露", "隐私信息贩卖", "个人档案曝光", "未授权访问"],
            "description": "数据保护法规"
        },
        
        # 印尼UU ITE修订条款
        "侮辱诽谤": {
            "level": "法律风险级",
            "words": ["人身攻击", "恶意中伤", "名誉损害", "诽谤他人", "侮辱性语言"],
            "description": "印尼ITE法第27条"
        },
        "恐吓威胁": {
            "level": "法律风险级",
            "words": ["死亡威胁", "暴力恐吓", "人身安全威胁", "报复威胁"],
            "description": "印尼ITE法第29条"
        },
        
        # 通用合规类别
        "色情低俗": {
            "level": "拦截级",
            "words": ["露骨内容", "成人服务", "色情表演", "性交易", "淫秽图片"],
            "description": "全球通用内容标准"
        },
        "欺诈诈骗": {
            "level": "高风险拦截级",
            "words": ["金融诈骗", "虚假投资", "传销组织", "网络钓鱼", "身份盗用"],
            "description": "金融犯罪防范"
        },
        "医疗健康": {
            "level": "替换级",
            "words": ["自制药物", "诊断建议", "治疗方案", "医疗偏方", "药物推荐"],
            "description": "医疗健康风险"
        },
        "金融投资": {
            "level": "警告级",
            "words": ["投资建议", "股票推荐", "理财产品", "投资回报", "金融产品"],
            "description": "金融投资风险"
        },
        "法律咨询": {
            "level": "警告级",
            "words": ["法律意见", "诉讼建议", "合同审查", "法律责任", "司法程序"],
            "description": "法律咨询风险"
        },
        "网络安全": {
            "level": "高风险拦截级",
            "words": ["黑客攻击", "系统漏洞", "网络渗透", "恶意软件", "网络钓鱼"],
            "description": "网络安全威胁"
        },
        "自我伤害": {
            "level": "合规违规级",
            "words": ["自杀方法", "自我伤害", "轻生念头", "结束生命", "自残行为"],
            "description": "自我伤害风险"
        },
        "仇恨言论": {
            "level": "高风险拦截级",
            "words": ["仇恨煽动", "群体对立", "暴力煽动", "仇外心理", "极端思想"],
            "description": "仇恨言论管控"
        }
    }
    
    total_created = 0
    for category_name, word_data in sample_words.items():
        if category_name not in categories or word_data["level"] not in levels:
            continue
            
        category_id = categories[category_name]
        level_id = levels[word_data["level"]]
        
        for word in word_data["words"]:
            word_id = SensitiveWordService.create_sensitive_word(
                db=db,
                word=word,
                category_id=category_id,
                level_id=level_id,
                match_type="exact",
                description=f"{word_data['description']} - {word}",
                source="六地AI合规标准初始化",
                tenant_id=tenant_id,
                created_by=user_id
            )
            
            if word_id:
                total_created += 1
                print(f"✓ 创建敏感词: {word} ({category_name})")
            else:
                print(f"✗ 创建敏感词失败: {word}")
    
    print(f"共创建 {total_created} 个合规敏感词")


def init_content_security_words(db, categories: dict, levels: dict, tenant_id: str, user_id: str):
    """初始化内容安全检测敏感词 - 基于7大类内容安全检测标准"""
    
    # 基于内容安全检测标准的敏感词数据
    content_security_words = {
        # 1. 色情内容检测
        "色情成人内容": {
            "level": "高风险拦截级",
            "words": ["性器官描述", "性行为动作", "色情交易", "卖淫嫖娼", "AV影片", "成人网站"],
            "description": "pornographic_adult - 疑似色情内容，风险分85-100，已开启"
        },
        "性健康内容": {
            "level": "已关闭检测",
            "words": ["性知识科普", "性教育", "性健康咨询", "生理知识", "性心理"],
            "description": "sexual_terms - 疑似性健康内容，检测已关闭"
        },
        "低俗暗示内容": {
            "level": "中风险拦截级",
            "words": ["低俗擦边", "敏感部位", "聊骚话术", "软色情", "性暗示", "新茶到了"],
            "description": "sexual_suggestive - 疑似低俗内容，部分已开启"
        },
        "色情诱导提示": {
            "level": "高风险拦截级",
            "words": ["生成色情内容", "描述性行为", "色情场景设定", "成人内容创作"],
            "description": "sexual_prompts - 诱导生成色情内容提示词，已开启"
        },
        
        # 2. 涉政内容检测
        "政治人物": {
            "level": "合规违规级",
            "words": ["国家主席", "总理", "领导人暗喻", "家属代称", "英雄烈士歪曲"],
            "description": "political_figure - 疑似政治人物，核心内容已开启"
        },
        "政治实体": {
            "level": "已关闭检测",
            "words": ["中国共产党", "政治事件", "军事言论", "机关单位", "省级职务"],
            "description": "political_entity - 疑似政治实体，检测已关闭"
        },
        "敏感政治内容": {
            "level": "极高风险级",
            "words": ["政治历史事件", "舆论争议", "否定国家制度", "分裂国家言论", "反华组织"],
            "description": "political_n - 疑似敏感政治内容，已开启"
        },
        "涉政禁宣人物": {
            "level": "已关闭检测",
            "words": ["侵华战犯", "国民党战犯", "落马官员", "劣迹艺人"],
            "description": "political_p - 涉政禁宣人物，检测已关闭"
        },
        "涉政诱导提示": {
            "level": "极高风险级",
            "words": ["生成涉政内容", "政治观点表达", "敏感话题讨论", "政治立场"],
            "description": "political_prompts - 诱导生成涉政内容提示词，已开启"
        },
        "涉政专项保障": {
            "level": "合规违规级",
            "words": ["特定时间节点", "专项保障策略", "政治敏感期", "重大会议期间"],
            "description": "political_a - 涉政专项升级保障，已开启"
        },
        
        # 3. 暴恐内容检测
        "极端组织": {
            "level": "极高风险级",
            "words": ["ISIS", "基地组织", "恐怖组织", "极端主义团体", "分离主义组织"],
            "description": "violent_extremists - 疑似极端组织，已开启"
        },
        "极端主义内容": {
            "level": "极高风险级",
            "words": ["宣扬恐怖主义", "极端思想传播", "暴力革命", "圣战主义", "仇恨煽动"],
            "description": "violent_incidents - 疑似极端主义内容，已开启"
        },
        "武器弹药": {
            "level": "法律风险级",
            "words": ["核武器制造", "生化武器", "爆炸物制作", "枪支弹药", "管制刀具"],
            "description": "violent_weapons - 疑似武器弹药，已开启"
        },
        "暴力诱导提示": {
            "level": "极高风险级",
            "words": ["生成暴力内容", "血腥场面描述", "暴力方法指导", "伤害教程"],
            "description": "violent_prompts - 诱导生成暴力内容提示词，已开启"
        },
        
        # 4. 违禁内容检测
        "毒品相关": {
            "level": "法律风险级",
            "words": ["毒品制造", "精神药物滥用", "致幻剂", "海洛因", "冰毒", "吸毒工具"],
            "description": "contraband_drug - 疑似毒品相关，已开启"
        },
        "赌博相关": {
            "level": "高风险拦截级",
            "words": ["网络赌博", "赌博工具", "地下赌场", "博彩网站", "六合彩", "赌博平台"],
            "description": "contraband_gambling - 疑似赌博相关，已开启"
        },
        "违禁行为": {
            "level": "法律风险级",
            "words": ["制假售假", "信用卡套现", "花呗套现", "公积金套现", "洗钱技术", "证件伪造"],
            "description": "contraband_act - 疑似违禁行为，已开启"
        },
        "违禁工具": {
            "level": "高风险拦截级",
            "words": ["色情域名", "翻墙软件", "VPN工具", "代理教程", "暗网访问"],
            "description": "contraband_entity - 疑似违禁工具，已开启"
        },
        
        # 5. 不良内容检测
        "未成年不适内容": {
            "level": "极高风险级",
            "words": ["侵犯未成年", "儿童不良行为", "校园暴力", "儿童剥削", "未成年色情"],
            "description": "inappropriate_minor - 疑似未成年不适内容，已开启"
        },
        "偏见歧视内容": {
            "level": "高风险拦截级",
            "words": ["种族歧视", "性别歧视", "地域歧视", "职业歧视", "宗教歧视", "残疾歧视"],
            "description": "inappropriate_discrimination - 疑似偏见歧视内容，已开启"
        },
        "不良价值观内容": {
            "level": "中风险拦截级",
            "words": ["违反道德", "价值观扭曲", "三观不正", "负能量传播", "道德沦丧"],
            "description": "inappropriate_ethics - 疑似不良价值观内容，已开启"
        },
        "攻击辱骂内容": {
            "level": "中风险拦截级",
            "words": ["人身攻击", "恶意诅咒", "辱骂诽谤", "网络暴力", "恶意中伤"],
            "description": "inappropriate_profanity - 疑似攻击辱骂内容，已开启"
        },
        "低俗口头语内容": {
            "level": "已关闭检测",
            "words": ["辱骂口头语", "粗口脏话", "低俗用语", "不文明用词"],
            "description": "inappropriate_oral - 疑似低俗口头语，检测已关闭"
        },
        "封建迷信内容": {
            "level": "中风险替换级",
            "words": ["封建迷信", "算命占卜", "风水玄学", "鬼神论", "迷信活动"],
            "description": "inappropriate_superstition - 疑似封建迷信内容，已开启"
        },
        "无意义灌水内容": {
            "level": "已关闭检测",
            "words": ["无意义符号", "重复灌水", "乱码内容", "特殊字符占位"],
            "description": "inappropriate_nonsense - 疑似无意义灌水，检测已关闭"
        },
        
        # 6. 宗教内容检测 (全部已关闭)
        "佛教相关": {
            "level": "已关闭检测",
            "words": ["佛教教规", "佛经", "释迦牟尼", "观音菩萨", "佛教仪式"],
            "description": "religion_b - 疑似涉及佛教，检测已关闭"
        },
        "道教相关": {
            "level": "已关闭检测",
            "words": ["道教教规", "道德经", "老子", "太上老君", "道教仪式"],
            "description": "religion_t - 疑似涉及道教，检测已关闭"
        },
        "基督教相关": {
            "level": "已关闭检测",
            "words": ["基督教教规", "圣经", "耶稣基督", "天主教", "基督教仪式"],
            "description": "religion_c - 疑似涉及基督教，检测已关闭"
        },
        "伊斯兰教相关": {
            "level": "已关闭检测",
            "words": ["伊斯兰教规", "古兰经", "真主安拉", "穆罕默德", "伊斯兰仪式"],
            "description": "religion_i - 疑似涉及伊斯兰教，检测已关闭"
        },
        "印度教相关": {
            "level": "已关闭检测",
            "words": ["印度教教规", "湿婆神", "毗湿奴", "梵天", "印度教仪式"],
            "description": "religion_h - 疑似涉及印度教，检测已关闭"
        },
        
        # 7. 广告内容检测
        "站外引流": {
            "level": "中风险替换级",
            "words": ["引流到微信", "加QQ群", "关注抖音", "下载APP", "访问网站"],
            "description": "pt_to_sites - 疑似站外引流，已开启"
        },
        "网赚兼职广告": {
            "level": "已关闭检测",
            "words": ["兼职招聘", "网赚项目", "在家赚钱", "副业推荐", "轻松赚钱"],
            "description": "pt_by_recruitment - 疑似网赚兼职广告，检测已关闭"
        },
        "引流广告号": {
            "level": "中风险替换级",
            "words": ["加微信", "QQ群号", "电话联系", "网址链接", "邮箱地址"],
            "description": "pt_to_contact - 疑似引流广告号，部分已开启"
        },
        
        # 8. 隐私敏感检测
        "个人隐私信息": {
            "level": "高风险拦截级",
            "words": ["账号密码", "身份证号", "银行卡号", "家庭住址", "手机号码", "个人邮箱"],
            "description": "privacy_p - 疑似涉及个人隐私信息，已开启"
        },
        "商业敏感内容": {
            "level": "高风险拦截级",
            "words": ["商业机密", "版权内容", "商业秘密", "内部资料", "财务数据"],
            "description": "privacy_b - 疑似涉及商业敏感内容，已开启"
        }
    }
    
    total_created = 0
    for category_name, word_data in content_security_words.items():
        if category_name not in categories or word_data["level"] not in levels:
            continue
            
        category_id = categories[category_name]
        level_id = levels[word_data["level"]]
        
        for word in word_data["words"]:
            word_id = SensitiveWordService.create_sensitive_word(
                db=db,
                word=word,
                category_id=category_id,
                level_id=level_id,
                match_type="exact",
                description=f"{word_data['description']} - {word}",
                source="内容安全检测标准初始化",
                tenant_id=tenant_id,
                created_by=user_id
            )
            
            if word_id:
                total_created += 1
                print(f"✓ 创建内容安全敏感词: {word} ({category_name})")
            else:
                print(f"✗ 创建内容安全敏感词失败: {word}")
    
    print(f"共创建 {total_created} 个内容安全检测敏感词")


def init_sample_whitelist(db, tenant_id: str, user_id: str):
    """初始化示例白名单 - 考虑合规豁免情况"""
    whitelist_words = [
        # 学术研究豁免
        {"word": "学术研究", "reason": "学术研究用途，符合研究豁免原则"},
        {"word": "科学实验", "reason": "科学实验用途，符合科研豁免"},
        {"word": "教育教学", "reason": "教育教学用途，符合教育豁免"},
        {"word": "历史文献", "reason": "历史文献引用，符合历史记录豁免"},
        
        # 技术术语豁免
        {"word": "技术术语", "reason": "专业技术术语，非敏感语境"},
        {"word": "医学术语", "reason": "医学专业术语，非诊疗建议"},
        {"word": "法律术语", "reason": "法律专业术语，非法律咨询"},
        {"word": "金融术语", "reason": "金融专业术语，非投资建议"},
        
        # 新闻报道豁免
        {"word": "新闻报道", "reason": "新闻报道用途，符合新闻自由原则"},
        {"word": "时事评论", "reason": "时事评论用途，符合言论自由原则"},
        {"word": "公共信息", "reason": "公共信息披露，符合透明度原则"},
        
        # 文学艺术豁免
        {"word": "文学作品", "reason": "文学艺术作品，符合创作自由原则"},
        {"word": "艺术表达", "reason": "艺术表达用途，符合艺术自由原则"},
        {"word": "创意内容", "reason": "创意内容创作，符合创作豁免"},
        
        # 安全测试豁免
        {"word": "安全测试", "reason": "安全测试用途，符合安全审计需求"},
        {"word": "漏洞研究", "reason": "漏洞研究用途，符合网络安全研究"},
        {"word": "渗透测试", "reason": "渗透测试用途，符合安全评估需求"},
        
        # 合规审计豁免
        {"word": "合规审计", "reason": "合规审计用途，符合监管要求"},
        {"word": "风险评估", "reason": "风险评估用途，符合风险管理需求"},
        {"word": "内部审查", "reason": "内部审查用途，符合内控要求"}
    ]
    
    created_count = 0
    for item in whitelist_words:
        whitelist_id = SensitiveWordWhitelistService.create_whitelist_word(
            db=db,
            word=item["word"],
            reason=item["reason"],
            tenant_id=tenant_id,
            created_by=user_id
        )
        
        if whitelist_id:
            created_count += 1
            print(f"✓ 创建白名单: {item['word']}")
        else:
            print(f"✗ 创建白名单失败: {item['word']}")
    
    print(f"共创建 {created_count} 个白名单词汇")


def init_region_specific_words(db, categories: dict, levels: dict, tenant_id: str, user_id: str, region: str = "global"):
    """根据不同地区初始化特定敏感词"""
    
    region_words = {
        "china": {
            "政治敏感": {
                "level": "合规违规级",
                "words": ["六四事件", "法轮功", "天安门", "达赖喇嘛", "台独"],
                "description": "中国大陆特定政治敏感词"
            }
        },
        "eu": {
            "生物识别滥用": {
                "level": "合规违规级", 
                "words": ["大规模监控", "预测性执法", "情绪操控", "无差别扫描"],
                "description": "欧盟AI法案特定禁止用途"
            }
        },
        "us": {
            "提示注入攻击": {
                "level": "高风险拦截级",
                "words": ["system prompt", "jailbreak", "prompt injection", "model extraction"],
                "description": "美国NIST特定攻击模式"
            }
        },
        "hk": {
            "个人隐私": {
                "level": "拦截级",
                "words": ["香港身份证", "BNO护照", "银行户口", "住址资料"],
                "description": "香港PDPO特定隐私保护"
            }
        },
        "malaysia": {
            "宗教敏感": {
                "level": "高风险拦截级",
                "words": ["宗教冲突", "种族矛盾", "宗教歧视", "族群对立"],
                "description": "马来西亚多元文化敏感词"
            }
        },
        "indonesia": {
            "侮辱诽谤": {
                "level": "法律风险级",
                "words": ["总统侮辱", "政府诽谤", "官员中伤", "国家形象损害"],
                "description": "印尼ITE法特定条款"
            }
        }
    }
    
    if region not in region_words:
        print(f"地区 {region} 暂无特定敏感词配置")
        return
    
    words_data = region_words[region]
    total_created = 0
    
    for category_name, word_data in words_data.items():
        if category_name not in categories or word_data["level"] not in levels:
            continue
            
        category_id = categories[category_name]
        level_id = levels[word_data["level"]]
        
        for word in word_data["words"]:
            word_id = SensitiveWordService.create_sensitive_word(
                db=db,
                word=word,
                category_id=category_id,
                level_id=level_id,
                match_type="exact",
                description=f"{word_data['description']} - {word}",
                source=f"{region.upper()}地区特定初始化",
                tenant_id=tenant_id,
                created_by=user_id
            )
            
            if word_id:
                total_created += 1
                print(f"✓ 创建{region}特定敏感词: {word} ({category_name})")
            else:
                print(f"✗ 创建{region}特定敏感词失败: {word}")
    
    print(f"共创建 {total_created} 个{region}地区特定敏感词")


def main():
    """主函数"""
    print("开始初始化敏感词库数据 - 基于内容安全检测标准和国际AI合规要求...")
    
    # 这里需要提供一个有效的租户ID和用户ID
    # 在实际使用中，可以从命令行参数或配置文件获取
    tenant_id = input("请输入租户ID: ").strip()
    user_id = input("请输入用户ID: ").strip()
    
    # 可选：选择特定地区
    region = input("请输入特定地区 (china/eu/us/hk/malaysia/indonesia/global): ").strip().lower()
    if not region:
        region = "global"
    
    # 可选：选择初始化类型
    print("\n请选择初始化类型:")
    print("1. 内容安全检测标准 (基于7大类检测标准)")
    print("2. 国际AI合规要求 (基于六地AI法规)")
    print("3. 全部初始化 (推荐)")
    choice = input("请输入选择 (1/2/3): ").strip()
    if not choice:
        choice = "3"
    
    if not tenant_id or not user_id:
        print("租户ID和用户ID不能为空")
        return
    
    db = SessionLocal()
    try:
        print(f"为租户 {tenant_id} 初始化敏感词库 (地区: {region}, 类型: {choice})...")
        
        # 1. 初始化分类
        print("\n1. 初始化敏感词分类...")
        categories = init_default_categories(db, tenant_id, user_id)
        
        # 2. 初始化等级
        print("\n2. 初始化敏感词等级...")
        levels = init_default_levels(db, tenant_id)
        
        # 3. 根据选择初始化敏感词
        if choice == "1":
            print("\n3. 初始化内容安全检测敏感词...")
            init_content_security_words(db, categories, levels, tenant_id, user_id)
        elif choice == "2":
            print("\n3. 初始化国际AI合规敏感词...")
            init_sample_sensitive_words(db, categories, levels, tenant_id, user_id)
        else:  # choice == "3" 或其他
            print("\n3a. 初始化内容安全检测敏感词...")
            init_content_security_words(db, categories, levels, tenant_id, user_id)
            print("\n3b. 初始化国际AI合规敏感词...")
            init_sample_sensitive_words(db, categories, levels, tenant_id, user_id)
        
        # 4. 初始化地区特定敏感词
        if region != "global":
            print(f"\n4. 初始化{region}地区特定敏感词...")
            init_region_specific_words(db, categories, levels, tenant_id, user_id, region)
        
        # 5. 初始化白名单
        print("\n5. 初始化白名单...")
        init_sample_whitelist(db, tenant_id, user_id)
        
        db.commit()
        print(f"\n✅ 敏感词库初始化完成！(地区: {region}, 类型: {choice})")
        
        print("\n📋 内容安全检测覆盖范围:")
        print("  🔴 色情内容检测: pornographic_adult, sexual_terms, sexual_suggestive, sexual_prompts")
        print("  🔴 涉政内容检测: political_figure, political_entity, political_n, political_p, political_prompts, political_a")
        print("  🔴 暴恐内容检测: violent_extremists, violent_incidents, violent_weapons, violent_prompts")
        print("  🔴 违禁内容检测: contraband_drug, contraband_gambling, contraband_act, contraband_entity")
        print("  🔴 不良内容检测: inappropriate_minor, inappropriate_discrimination, inappropriate_ethics, inappropriate_profanity, inappropriate_oral, inappropriate_superstition, inappropriate_nonsense")
        print("  🔴 宗教内容检测: religion_b, religion_t, religion_c, religion_i, religion_h")
        print("  🔴 广告内容检测: pt_to_sites, pt_by_recruitment, pt_to_contact")
        print("  🔴 隐私敏感检测: privacy_p, privacy_b")
        
        print("\n📋 国际AI合规覆盖范围:")
        print("  • 中国 TC260-003 生成式AI服务安全要求")
        print("  • 欧盟 AI法案 (Reg. EU 2024/1689)")
        print("  • 美国 NIST AI 100-2e 安全标准")
        print("  • 香港 AI道德标准指引")
        print("  • 马来西亚 AI治理框架")
        print("  • 印尼 电子信息和交易法修订")
        
        print("\n📊 风险分级对照:")
        print("  🟢 监控级 (0-49分): 仅记录，不拦截")
        print("  🟡 警告级 (50-69分): 警告提示")
        print("  🟠 中风险 (70-84分): 替换或部分拦截")
        print("  🔴 高风险 (85-94分): 直接拦截")
        print("  ⚫ 极高风险 (95-100分): 拦截并上报")
        print("  ⭕ 法律/合规风险: 最高级别拦截")
        print("  ⚪ 已关闭检测: 检测状态已关闭")
        
    except Exception as e:
        db.rollback()
        print(f"\n❌ 初始化失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()


if __name__ == "__main__":
    main()