"""
简单测试示例
使用模拟的文本块测试 Constellation 核心功能
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from infrastructure.models import Block
from modules.parser.parser import CaliperParser


def create_sample_blocks():
    """创建示例文本块"""
    blocks = [
        Block(id=0, type="text", text="深度学习技术综述"),
        Block(id=1, type="text", text="本文介绍了深度学习的基本概念和最新进展。深度学习是机器学习的一个分支，通过多层神经网络来学习数据的表示。"),
        Block(id=2, type="text", text="第一章 基础理论"),
        Block(id=3, type="text", text="深度学习的理论基础包括神经网络、反向传播算法和梯度下降优化方法。这些基础理论为深度学习的发展奠定了坚实的基础。"),
        Block(id=4, type="text", text="1.1 神经网络结构"),
        Block(id=5, type="text", text="神经网络由输入层、隐藏层和输出层组成。每一层包含多个神经元，神经元之间通过权重连接。激活函数用于引入非线性特性。"),
        Block(id=6, type="image", caption="图1: 神经网络结构示意图", image_data="neural_network.png"),
        Block(id=7, type="text", text="1.2 反向传播算法"),
        Block(id=8, type="text", text="反向传播算法是训练神经网络的核心方法。它通过计算损失函数对权重的梯度，然后使用梯度下降法更新权重。"),
        Block(id=9, type="text", text="第二章 应用实践"),
        Block(id=10, type="text", text="深度学习在计算机视觉、自然语言处理、语音识别等领域取得了突破性进展。"),
        Block(id=11, type="text", text="2.1 计算机视觉"),
        Block(id=12, type="text", text="卷积神经网络（CNN）在图像分类、目标检测和图像分割任务中表现出色。ResNet、VGG等经典架构被广泛应用。"),
        Block(id=13, type="text", text="2.2 自然语言处理"),
        Block(id=14, type="text", text="Transformer架构和预训练模型（如BERT、GPT）彻底改变了NLP领域。这些模型在文本分类、机器翻译、问答系统等任务中达到了前所未有的性能。"),
        Block(id=15, type="text", text="第三章 未来展望"),
        Block(id=16, type="text", text="深度学习的未来发展方向包括更高效的模型架构、更少的数据需求、更强的可解释性以及与其他AI技术的融合。"),
    ]
    return blocks


def main():
    """主函数"""
    print("=" * 60)
    print("Constellation 简单测试")
    print("=" * 60)
    
    # 创建示例数据
    print("\n[1/4] 创建示例文本块...")
    blocks = create_sample_blocks()
    print(f"   创建了 {len(blocks)} 个文本块")
    
    # 初始化解析器
    print("\n[2/4] 初始化 Constellation 解析器...")
    parser = CaliperParser()
    print("   解析器初始化完成")
    
    # 执行解析
    print("\n[3/4] 开始解析文档结构...")
    print("   - 压缩骨架文本")
    print("   - 调用 LLM 标注章节")
    print("   - 强制闭合区间")
    print("   - 组装文档树")
    
    try:
        document_tree = parser.parse(blocks)
        print("   解析成功！")
    except Exception as e:
        print(f"   解析失败: {e}")
        return
    
    # 输出结果
    print("\n[4/4] 输出解析结果...")
    print("\n" + "=" * 60)
    print("JSON 格式输出:")
    print("=" * 60)
    print(document_tree.to_json(indent=2))
    
    print("\n" + "=" * 60)
    print("Markdown 格式输出:")
    print("=" * 60)
    print(document_tree.to_markdown())
    
    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
