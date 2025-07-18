# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #

from os import PathLike, remove, unlink
from os.path import exists
from pathlib import Path

from testing import assert_false, assert_raises, assert_true


fn create_file_and_test_delete_path[
    func: fn[PathLike: PathLike] (PathLike) raises -> None,
    name: StaticString,
](filepath: Path) raises:
    try:
        with open(filepath.__fspath__(), "w"):
            pass
    except:
        assert_true(False, "Failed to create file for test")

    assert_true(exists(filepath))
    func(filepath)
    assert_false(exists(filepath), "test with '" + name + "' failed")


fn test_remove() raises:
    var cwd_path = Path()
    var my_file_path = cwd_path / "my_file.test"
    var my_file_name = String(my_file_path)

    # verify that the test file does not exist before starting the test
    assert_false(
        exists(my_file_name),
        "Unexpected file " + my_file_name + " it should not exist",
    )

    # trying to delete non existing file
    with assert_raises(contains="Can not remove file: "):
        remove(my_file_name)
    with assert_raises(contains="Can not remove file: "):
        remove(my_file_path)

    create_file_and_test_delete_path[remove, "remove"](my_file_name)
    create_file_and_test_delete_path[unlink, "unlink"](my_file_name)
    create_file_and_test_delete_path[remove, "remove"](my_file_path)
    create_file_and_test_delete_path[unlink, "unlink"](my_file_path)

    # test with relative path
    my_file_name = String(Path("my_relative_file.test"))
    create_file_and_test_delete_path[remove, "remove"](my_file_name)


def main():
    test_remove()
