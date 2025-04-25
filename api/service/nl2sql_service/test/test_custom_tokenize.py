import os

import jieba
import pytest
from unittest import mock
import tempfile

from api.service.nl2sql_service.custom_jieba_tokenizer import custom_tokenize, fetch_custom_words_from_api, \
    write_words_to_file


# 导入被测试的模块
# 假设你的模块路径为 nlp.tokenizer，需要根据实际情况调整
# from nlp.tokenizer import custom_tokenize, fetch_custom_words_from_api, write_words_to_file

# 直接导入测试目标函数


class TestCustomTokenize:
    """测试自定义分词功能"""

    def test_custom_tokenize_normal_case(self):
        """测试正常情况下的分词功能"""
        text = "计算机科学与技术学院的老师在计算机学院大楼讲课"
        domains = ["1", "2"]

        # 模拟API响应
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 2,
                "keywordList": ["计算机学院", "计算机科学与技术学院"]
            }
        }

        with mock.patch('requests.get', return_value=mock_response) as mock_post:
            result = custom_tokenize(text, domains)

            # 验证API调用参数
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            assert 'json' in kwargs
            assert kwargs['json']['domainList'] == domains

            # 验证分词结果
            assert isinstance(result, list)
            assert "计算机科学与技术学院" in result
            assert "计算机学院" in result
            assert "讲课" in result

    def test_custom_tokenize_with_domains_and_datasets(self):
        """测试同时指定domains和datasets参数"""
        text = "示例文本"
        domains = ["1"]
        datasets = ["dataset1"]

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 1,
                "keywordList": ["示例"]
            }
        }

        with mock.patch('requests.get', return_value=mock_response) as mock_post:
            result = custom_tokenize(text, domains, datasets)

            # 验证API调用参数
            args, kwargs = mock_post.call_args
            assert kwargs['json']['domainList'] == domains
            assert kwargs['json']['datasetList'] == datasets

    def test_custom_tokenize_empty_text(self):
        """测试空文本输入"""
        text = ""

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 1,
                "keywordList": ["示例词"]
            }
        }

        with mock.patch('requests.get', return_value=mock_response):
            result = custom_tokenize(text)

            # 空文本应返回空列表
            assert result == []

    def test_custom_tokenize_api_error(self):
        """测试API返回错误码的情况"""
        text = "示例文本"

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 500,
            "message": "服务器错误",
            "data": None
        }

        with mock.patch('requests.get', return_value=mock_response):
            result = custom_tokenize(text)

            # API错误应该返回默认分词结果（不使用自定义词典）
            assert isinstance(result, list)
            assert len(result) > 0  # 至少应该有一些分词结果

    def test_custom_tokenize_api_exception(self):
        """测试API调用异常的情况"""
        text = "示例文本"

        with mock.patch('requests.get', side_effect=Exception("API连接失败")):
            result = custom_tokenize(text)

            # 异常情况下应该返回默认分词结果
            assert isinstance(result, list)
            assert len(result) > 0

    def test_custom_tokenize_no_custom_words(self):
        """测试没有自定义词的情况"""
        text = "简单的示例文本"

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 0,
                "keywordList": []
            }
        }

        with mock.patch('requests.get', return_value=mock_response):
            result = custom_tokenize(text)

            # 验证使用默认分词
            assert isinstance(result, list)
            assert len(result) > 0


class TestFetchCustomWordsFromAPI:
    """测试从API获取自定义词的功能"""

    def test_fetch_custom_words_single_page(self):
        """测试获取单页词汇"""
        domains = ["1"]
        datasets = []

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 3,
                "keywordList": ["词1", "词2", "词3"]
            }
        }

        with mock.patch('requests.get', return_value=mock_response) as mock_post:
            result = fetch_custom_words_from_api(domains, datasets)

            # 验证API调用参数和返回结果
            assert mock_post.call_count == 1
            assert len(result) == 3
            assert result == ["词1", "词2", "词3"]

    def test_fetch_custom_words_api_error(self):
        """测试API返回错误码的情况"""
        domains = ["1"]
        datasets = []

        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 500,
            "message": "服务器错误",
            "data": None
        }

        with mock.patch('requests.get', return_value=mock_response):
            result = fetch_custom_words_from_api(domains, datasets)

            # 错误码应返回空列表
            assert result == []

    def test_fetch_custom_words_exception(self):
        """测试API调用异常的情况"""
        domains = ["1"]
        datasets = []

        with mock.patch('requests.get', side_effect=Exception("API连接失败")):
            result = fetch_custom_words_from_api(domains, datasets)

            # 异常情况应返回空列表
            assert result == []


class TestWriteWordsToFile:
    """测试将词列表写入文件的功能"""

    def test_write_words_normal_case(self):
        """测试正常写入词汇到文件"""
        words = ["词1", "词2", "词3"]

        # 使用临时目录以避免实际文件IO
        with mock.patch('builtins.open', mock.mock_open()) as mock_file:
            file_path = write_words_to_file(words)

            # 验证文件操作
            assert file_path == "temp_custom_dict.txt"
            mock_file.assert_called_once_with("temp_custom_dict.txt", 'w', encoding='utf-8')

            # 验证写入内容
            file_handle = mock_file()
            expected_calls = [mock.call(f"{word}\n") for word in words]
            file_handle.write.assert_has_calls(expected_calls)

    def test_write_words_empty_list(self):
        """测试写入空词列表"""
        words = []

        file_path = write_words_to_file(words)

        # 空列表应返回空字符串，不创建文件
        assert file_path == ""

    def test_write_words_exception(self):
        """测试写文件异常的情况"""
        words = ["词1", "词2"]

        with mock.patch('builtins.open', side_effect=Exception("文件写入错误")):
            file_path = write_words_to_file(words)

            # 异常情况应返回空字符串
            assert file_path == ""


# 集成测试，测试完整流程
class TestCustomTokenizeIntegration:
    """集成测试自定义分词流程"""

    @pytest.mark.integration
    def test_custom_tokenize_real_file_operations(self):
        """测试实际文件操作的分词流程（仍使用模拟API）"""
        text = "计算机科学与技术学院的老师在计算机学院大楼讲课"
        domains = ["1", "2"]

        # 模拟API响应
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 2,
                "keywordList": ["计算机学院", "计算机科学与技术学院"]
            }
        }

        # 使用模拟API但真实文件操作
        # 不模拟write_words_to_file和os.remove以测试真实文件操作
        with mock.patch('requests.get', return_value=mock_response):
            result = custom_tokenize(text, domains)

            # 验证分词结果
            assert isinstance(result, list)
            assert "计算机科学与技术学院" in result
            assert "计算机学院" in result
            assert "讲课" in result

            # 确保没有遗留临时文件
            assert not os.path.exists("temp_custom_dict.txt"), "临时文件未被删除"

    @pytest.mark.integration
    def test_custom_tokenize_real_jieba_operations(self):
        """测试实际文件操作和真实jieba分词（仍使用模拟API）"""
        text = "北京大学是中国最著名的高等学府之一"
        domains = ["education"]

        # 自定义词列表
        custom_words = ["北京大学", "高等学府"]

        # 使用模拟API返回自定义词
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": len(custom_words),
                "keywordList": custom_words
            }
        }

        # 仅模拟API调用，使用真实jieba分词和文件操作
        with mock.patch('requests.get', return_value=mock_response):
            result = custom_tokenize(text, domains)

            # 验证分词结果
            assert "北京大学" in result
            assert "高等学府" in result

            # 确保没有遗留临时文件
            assert not os.path.exists("temp_custom_dict.txt"), "临时文件未被删除"

    @pytest.mark.real_api
    def test_custom_tokenize_with_real_api(self):
        """
        使用真实API进行集成测试（需要实际API环境）

        注意：此测试默认被跳过，需要显式运行:
        pytest test_custom_tokenize.py::TestCustomTokenizeIntegration::test_custom_tokenize_with_real_api -v
        """
        # 跳过此测试，除非明确要求运行
        # pytest.skip("默认跳过真实API测试，需要显式运行")

        text = "计算机科学与技术学院的老师在计算机学院大楼讲课"
        domains = ["1", "2"]  # 根据实际可用域调整

        # 不使用任何模拟，进行真实API调用和文件操作
        result = custom_tokenize(text, domains)

        # 基本验证
        assert isinstance(result, list)
        assert len(result) > 0
        print(f"实际分词结果: {result}")

        # 检查是否有残留的临时文件
        assert not os.path.exists("temp_custom_dict.txt"), "临时文件未被删除"

    @pytest.fixture
    def custom_words_file(self):
        """创建一个包含自定义词的临时文件"""
        words = ["计算机学院", "计算机科学与技术学院", "讲课"]

        # 创建临时文件并写入词汇
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as f:
            for word in words:
                f.write(f"{word}\n")
            temp_path = f.name

        yield temp_path

        # 测试后清理
        if os.path.exists(temp_path):
            os.remove(temp_path)

    def test_custom_tokenize_with_fixture_file(self, custom_words_file):
        """使用真实的自定义词文件测试分词"""
        text = "计算机科学与技术学院的老师在计算机学院大楼讲课"

        # 直接使用自定义词文件，绕过API调用
        with mock.patch('api.service.nl2sql_service.custom_jieba_tokenizer.fetch_custom_words_from_api',
                        return_value=[]), \
                mock.patch('api.service.nl2sql_service.custom_jieba_tokenizer.write_words_to_file',
                           return_value=custom_words_file):
            result = custom_tokenize(text)

            # 验证分词结果
            assert "计算机科学与技术学院" in result
            assert "计算机学院" in result
            assert "讲课" in result

    def test_cleanup_on_exception(self):
        """测试在处理过程中发生异常时的清理操作"""
        text = "测试文本"

        # 模拟API响应
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "code": 0,
            "message": "成功",
            "data": {
                "total": 1,
                "keywordList": ["测试"]
            }
        }

        # 创建实际临时文件
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file_path = temp_file.name

            # 写入一些内容确保文件存在
            temp_file.write(b"test content")

        # 确保文件存在
        assert os.path.exists(temp_file_path)

        try:
            # 模拟切词异常，测试是否正确清理文件
            with mock.patch('requests.get', return_value=mock_response), \
                    mock.patch('api.service.nl2sql_service.custom_jieba_tokenizer.write_words_to_file',
                               return_value=temp_file_path), \
                    mock.patch.object(jieba.Tokenizer, 'cut', side_effect=Exception("分词异常")):

                with pytest.raises(Exception):
                    custom_tokenize(text)

                # 检查文件是否被清理
                assert not os.path.exists(temp_file_path), "异常情况下临时文件未被清理"
        finally:
            # 以防测试失败，确保清理
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
