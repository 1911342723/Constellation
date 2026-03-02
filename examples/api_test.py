"""
Cursor-Caliper API 测试示例
演示如何调用 API 进行文档解析
"""
import requests
import json


def test_health_check():
    """测试健康检查接口"""
    print("=" * 60)
    print("测试 1: 健康检查")
    print("=" * 60)
    
    response = requests.get("http://localhost:8001/api/v1/health")
    print(f"状态码: {response.status_code}")
    print(f"响应: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    print()


def test_parse_blocks():
    """测试通用解析接口"""
    print("=" * 60)
    print("测试 2: 通用文档解析")
    print("=" * 60)
    
    # 准备测试数据
    blocks = [
        {"id": 0, "type": "text", "text": "深度学习技术综述"},
        {"id": 1, "type": "text", "text": "本文介绍了深度学习的基本概念和最新进展。"},
        {"id": 2, "type": "text", "text": "第一章 基础理论"},
        {"id": 3, "type": "text", "text": "深度学习的理论基础包括神经网络、反向传播算法等。"},
        {"id": 4, "type": "text", "text": "1.1 神经网络结构"},
        {"id": 5, "type": "text", "text": "神经网络由输入层、隐藏层和输出层组成。"},
        {"id": 6, "type": "text", "text": "第二章 应用实践"},
        {"id": 7, "type": "text", "text": "深度学习在计算机视觉、自然语言处理等领域取得了突破。"}
    ]
    
    response = requests.post(
        "http://localhost:8001/api/v1/parse",
        json={
            "blocks": blocks,
            "title": "深度学习技术综述",
            "authors": "张三"
        }
    )
    
    print(f"状态码: {response.status_code}")
    result = response.json()
    
    if result["success"]:
        print("✓ 解析成功")
        print(f"\n文档树结构 (共 {len(result['document_tree'])} 个节点):")
        for node in result['document_tree']:
            indent = "  " * (node['level'] - 1) if node['level'] > 0 else ""
            print(f"{indent}- {node['title']} (level {node['level']})")
        
        print(f"\nMarkdown 输出预览:")
        print(result['markdown'][:500] + "..." if len(result['markdown']) > 500 else result['markdown'])
    else:
        print("✗ 解析失败")
    print()


def test_parse_for_paper_editor():
    """测试文章排版系统专用接口"""
    print("=" * 60)
    print("测试 3: 文章排版系统专用解析")
    print("=" * 60)
    
    # 准备测试数据
    blocks = [
        {"id": 0, "type": "text", "text": "人工智能发展报告"},
        {"id": 1, "type": "text", "text": "本报告全面分析了人工智能技术的发展现状和未来趋势。"},
        {"id": 2, "type": "text", "text": "第一章 技术现状"},
        {"id": 3, "type": "text", "text": "当前人工智能技术已经在多个领域取得重大突破。"},
        {"id": 4, "type": "text", "text": "1.1 机器学习"},
        {"id": 5, "type": "text", "text": "机器学习是人工智能的核心技术之一。"},
        {"id": 6, "type": "text", "text": "1.2 深度学习"},
        {"id": 7, "type": "text", "text": "深度学习推动了计算机视觉和自然语言处理的发展。"},
        {"id": 8, "type": "text", "text": "第二章 应用场景"},
        {"id": 9, "type": "text", "text": "人工智能在医疗、金融、教育等领域有广泛应用。"},
        {"id": 10, "type": "text", "text": "第三章 未来展望"},
        {"id": 11, "type": "text", "text": "未来人工智能将向更加通用和智能的方向发展。"}
    ]
    
    response = requests.post(
        "http://localhost:8001/api/v1/parse/paper",
        json={
            "blocks": blocks,
            "title": "人工智能发展报告",
            "authors": "李四"
        }
    )
    
    print(f"状态码: {response.status_code}")
    result = response.json()
    
    if result["success"]:
        paper_data = result["paper_data"]
        print("✓ 解析成功")
        print(f"\n文档信息:")
        print(f"  标题: {paper_data['title']}")
        print(f"  作者: {paper_data['authors']}")
        print(f"  模板: {paper_data['schoolTemplate']}")
        print(f"\n章节列表 (共 {len(paper_data['sections'])} 个):")
        
        for section in paper_data['sections']:
            indent = "  " * section['level'] if section['level'] > 0 else ""
            section_type = f"[{section['type']}]" if section['type'] != 'section' else ""
            content_preview = section['content'][:50] + "..." if len(section['content']) > 50 else section['content']
            print(f"{indent}- {section['title']} {section_type}")
            if content_preview:
                print(f"{indent}  内容: {content_preview}")
    else:
        print("✗ 解析失败")
    print()


def test_with_images_and_tables():
    """测试包含图片和表格的文档"""
    print("=" * 60)
    print("测试 4: 包含多媒体内容的文档")
    print("=" * 60)
    
    blocks = [
        {"id": 0, "type": "text", "text": "实验报告"},
        {"id": 1, "type": "text", "text": "本实验研究了深度学习模型的性能。"},
        {"id": 2, "type": "text", "text": "第一章 实验设计"},
        {"id": 3, "type": "text", "text": "我们设计了以下实验方案。"},
        {"id": 4, "type": "image", "image_data": "https://example.com/image1.png", "caption": "图1: 实验流程图"},
        {"id": 5, "type": "text", "text": "第二章 实验结果"},
        {"id": 6, "type": "text", "text": "实验结果如下表所示。"},
        {"id": 7, "type": "table", "caption": "表1: 性能对比", "table_data": {"html": "<table>...</table>"}},
        {"id": 8, "type": "text", "text": "第三章 结论"},
        {"id": 9, "type": "text", "text": "实验证明了该方法的有效性。"}
    ]
    
    response = requests.post(
        "http://localhost:8001/api/v1/parse/paper",
        json={
            "blocks": blocks,
            "title": "实验报告",
            "authors": "王五"
        }
    )
    
    print(f"状态码: {response.status_code}")
    result = response.json()
    
    if result["success"]:
        paper_data = result["paper_data"]
        print("✓ 解析成功")
        print(f"\n多媒体内容统计:")
        
        image_count = sum(1 for s in paper_data['sections'] if '![' in s.get('content', ''))
        table_count = sum(1 for s in paper_data['sections'] if '[Table:' in s.get('content', ''))
        
        print(f"  图片数量: {image_count}")
        print(f"  表格数量: {table_count}")
        print(f"  章节数量: {len([s for s in paper_data['sections'] if s['type'] == 'section'])}")
    else:
        print("✗ 解析失败")
    print()


def main():
    """运行所有测试"""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 15 + "Cursor-Caliper API 测试" + " " * 20 + "║")
    print("╚" + "=" * 58 + "╝")
    print()
    
    try:
        # 测试 1: 健康检查
        test_health_check()
        
        # 测试 2: 通用解析
        test_parse_blocks()
        
        # 测试 3: 文章排版系统专用
        test_parse_for_paper_editor()
        
        # 测试 4: 多媒体内容
        test_with_images_and_tables()
        
        print("=" * 60)
        print("✓ 所有测试完成")
        print("=" * 60)
        
    except requests.exceptions.ConnectionError:
        print("\n✗ 错误: 无法连接到 Cursor-Caliper API")
        print("请确保服务已启动: python app/main.py")
    except Exception as e:
        print(f"\n✗ 测试失败: {str(e)}")


if __name__ == "__main__":
    main()
