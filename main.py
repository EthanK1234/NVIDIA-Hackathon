"""Run the agent loop on an example task. Replace `TASK` with your own."""

import hashlib
import re
from pathlib import Path
from agents.controller import run
from agents.verifier import verify


def _confidence(result: dict, verify_result: dict = None) -> int:
    """Estimate solution confidence as a percentage.

    Test score (primary signal):
      - success:            95 − 3 per extra iteration (floor 72)
      - tests pass, no approve: 65
      - partial pass:       up to 65, scaled by pass rate
      - nothing ran:        10  (likely broken test file)

    Verifier score (independent signal, weighted 2:1 over test score):
      - VERIFIED:           88  — blended in, or +3 on top of a success run
      - LIKELY_WRONG:       hard cap at 35 regardless of tests
      - UNCERTAIN / absent: test score only
    """
    history = result["history"]
    if not history:
        return 0

    last = history[-1]
    summary = last["exec_summary"]

    passed_count = len(re.findall(r"test_solution\.py::\S+ PASSED", summary))
    failed_count = len(re.findall(r"FAILED test_solution\.py::", summary))
    total_count = passed_count + failed_count

    if result["status"] == "success":
        test_score = max(72, 95 - (result["iterations"] - 1) * 3)
    elif last["passed"]:
        test_score = 65
    elif total_count > 0:
        test_score = max(15, int((passed_count / total_count) * 65))
    else:
        test_score = 10  # test file likely broken; no data

    if verify_result is None:
        return test_score

    verdict = verify_result["verdict"]

    if verdict == "LIKELY_WRONG":
        return min(test_score, 35)

    if verdict != "VERIFIED":
        return test_score

    # VERIFIED: independent confirmation — weight it 2:1 over test score
    if result["status"] == "success":
        return min(99, test_score + 3)
    return (test_score + 88 * 2) // 3

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

TASK = """
The median is the middle value in an ordered integer list. If the size of the list is even, there is no middle value. So the median is the mean of the two middle values.

For examples, if arr = [2,3,4], the median is 3.
For examples, if arr = [1,2,3,4], the median is (2 + 3) / 2 = 2.5.
You are given an integer array nums and an integer k. There is a sliding window of size k which is moving from the very left of the array to the very right. You can only see the k numbers in the window. Each time the sliding window moves right by one position.

Return the median array for each window in the original array. Answers within 10-5 of the actual value will be accepted.

Example 1:
Input: nums = [1,3,-1,-3,5,3,6,7], k = 3
Output: [1.00000,-1.00000,-1.00000,3.00000,5.00000,6.00000]

Constraints:
1 <= k <= nums.length <= 105
-231 <= nums[i] <= 231 - 1

CRITICAL PERFORMANCE REQUIREMENTS FOR PYTHON (AIMING FOR 100% RUNTIME PERFORMANCE):
1. Implementation Style: Write a standard LeetCode class solution snippet:
   class Solution:
       def medianSlidingWindow(self, nums: List[int], k: int) -> List[float]:

2. Algorithmic Optimization:
   - DO NOT use standard `collections.Counter` or naive heap lazy-deletions that leave huge amounts of dead elements around if it blows up memory/garbage collection. 
   - Optimize heap operations: Prefer inline code logic over nested helper function calls (e.g., `def add()`, `def remove()`) inside the main loop to completely eliminate Python's function call overhead.
   - Avoid `sortedcontainers.SortedList` if the benchmark demands beating 100%, as pure C-level arrays or highly optimized customized dual-heap arrays with aggressive pruning of root nodes are faster due to less internal object wrapping.
   - If using two heaps (max-heap `small`, min-heap `large`), cache heap-top values in local variables before processing branches where possible. Ensure that elements are aggressively popped from the heap tops the absolute second they become invalid.

3. Micro-optimizations for Python 3 execution:
   - Localize variables: Bind long global references to local names before entering loops (e.g., `heappush = heapq.heappush`, `heappop = heapq.heappop`).
   - Reduce division operations: When calculating medians for even sizes, scale or defer conversions to `float` until appending to the final array.
   - Pipelined conditional checks: Sequence your balancing branches so that the most common paths (where no balancing is required) short-circuit early.
your answer runs as fast as possible, and it is in LeetCode style. Try to achieve the best time complexity and fastest runtime.

"""


if __name__ == "__main__":
    task_hash = hashlib.md5(TASK.encode()).hexdigest()
    hash_path = OUTPUT_DIR / "task.hash"
    cached_tests_path = OUTPUT_DIR / "test_solution.py"

    if (cached_tests_path.exists() and hash_path.exists()
            and hash_path.read_text().strip() == task_hash):
        cached_tests = cached_tests_path.read_text()
    else:
        cached_tests = None
        if cached_tests_path.exists():
            print("[cache] task changed — discarding cached tests.")
    hash_path.write_text(task_hash)

    result = run(TASK, max_iterations=4, tests=cached_tests, samples=3)

    (OUTPUT_DIR / "solution.py").write_text(result["code"])
    (OUTPUT_DIR / "test_solution.py").write_text(result["tests"])

    verify_result = verify(TASK, result["code"])

    print(f"\n--- FINAL CODE (confidence: {_confidence(result, verify_result)}%) ---")
    print(result["code"])