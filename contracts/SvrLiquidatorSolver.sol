// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/*
 * SvrLiquidatorSolver —— Chainlink SVR / Atlas 拍卖的 solver 合约骨架
 *
 * ⚠️ 未部署、未审计、未实盘。这是学习用的骨架。
 *
 * ── 接口来源(全部链上核实过,不是照文档抄的)────────────────────
 *   atlasSolverCall(address,address,address,uint256,bytes,bytes) = 0x024181a6
 *     → 在 3 个真实 solver 合约的字节码里都找到了
 *   Atlas 上确认存在:reconcile(uint256) / shortfall() / borrow(uint256)
 *   Aave Pool 实现上确认:flashLoanSimple(...) = 0x42b0b77c
 *
 *   liquidationCall(address,address,address,uint256,bool) = 0x00a718a9
 *     ⚠️ 这个**字节码扫描没扫到,是假阴性**。改用 eth_call 试探才确认:
 *        拿一个健康仓位去调 → revert 0x930bb771
 *                            = HealthFactorNotBelowThreshold()
 *        对照:不存在的签名 revert 数据是 0x(空)
 *     教训:代理 / 模块化分发会让"扫字节码找选择器"漏掉真实存在的函数。
 *           **eth_call 试探比字节码扫描可靠** —— 看 revert 数据是否为空。
 *
 *   核实过程见 docs/week_3/SVR与Atlas教学.md 第 12 课
 *
 * ── 一次中标后发生什么 ────────────────────────────────────────
 *   Atlas 把 [预言机更新, 你的清算] 打包成一个 metacall
 *      → 预言机更新先执行,价格变了
 *      → Atlas 调你的 atlasSolverCall
 *          1. 闪电贷借出债务资产
 *          2. liquidationCall 还债、拿抵押物(打折)
 *          3. 把抵押物换回债务资产
 *          4. 还闪电贷
 *          5. 把中标价 bidAmount 付给执行环境
 *          6. 调 Atlas.reconcile 结清 gas
 *      → 任何一步 revert,整笔回滚,你只损失 gas(从押金里扣)
 *
 * ── 相对我们反编译的那个 bot,这里补上的东西 ──────────────────
 *   那个 bot(0xf0570Ec4…)**完全没有滑点保护** —— 它假设清算和变现
 *   一定按预期成交。本合约在两处强制下限:
 *     · minCollateralOut —— 清算实际拿到的抵押物不能少于预期
 *     · minDebtBack      —— 换回来的债务资产不能少于预期
 *   两处都不满足就 revert,整笔回滚。宁可不赚,不能亏。
 */

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function transfer(address, uint256) external returns (bool);
}

interface IAtlas {
    /// 结清本次执行欠 Atlas 的 gas。必须在执行结束前调用。
    function reconcile(uint256 maxApprovedGasSpend) external payable returns (uint256);
    /// 还差多少没结清
    function shortfall() external view returns (uint256);
}

interface IAavePool {
    function flashLoanSimple(
        address receiver,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;
}

contract SvrLiquidatorSolver {
    // ── 不可变配置 ────────────────────────────────────────────────
    address public immutable ATLAS;
    address public immutable AAVE_POOL;
    address public immutable OWNER;

    // ── 可变配置(仅 owner)──────────────────────────────────────
    /// 允许调用的 DEX router。**不做白名单的话,swapData 可以是任意调用,
    /// 等于把合约的授权权限交给出价者。**
    mapping(address => bool) public routerAllowed;

    /// 单笔出价上限。防止程序算错或被喂坏数据时把押金掏空。
    uint256 public maxBidWei;

    /// 本次 metacall 里允许 Atlas 扣的 gas 上限
    uint256 public maxGasSpendWei;

    // ── 清算参数(由链下估值程序编码进 solverOpData)──────────────
    struct LiqParams {
        address collateralAsset;   // 要拿的抵押物
        address debtAsset;         // 要还的债
        address user;              // 被清算的仓位
        uint256 debtToCover;       // 还多少债
        uint256 minCollateralOut;  // ★ 滑点保护 1:至少要拿到这么多抵押物
        address swapTarget;        // 变现用的 router(必须在白名单里)
        bytes   swapData;          // 变现的 calldata
        uint256 minDebtBack;       // ★ 滑点保护 2:至少要换回这么多债务资产
    }

    event Configured(address indexed router, bool allowed);
    event Liquidated(
        address indexed user,
        address collateralAsset,
        uint256 collateralOut,
        uint256 debtBack,
        uint256 bidPaid
    );

    error NotAtlas();
    error NotOwner();
    error NotSelf();
    error WrongSolverIdentity();
    error RouterNotAllowed();
    error BidTooHigh(uint256 asked, uint256 cap);
    error UnsupportedBidToken(address token);
    error CollateralShortfall(uint256 got, uint256 want);
    error ProceedsShortfall(uint256 got, uint256 want);
    error SwapFailed();
    error BidTransferFailed();

    modifier onlyOwner() {
        if (msg.sender != OWNER) revert NotOwner();
        _;
    }

    constructor(address atlas_, address aavePool_) {
        ATLAS = atlas_;
        AAVE_POOL = aavePool_;
        OWNER = msg.sender;
        maxBidWei = 0.05 ether;      // 保守起步。中标价见过 11 ETH 的,
                                     // 但那是别人的规模,我们先设小。
        maxGasSpendWei = 0.01 ether;
    }

    receive() external payable {}

    // ══════════════════════════════════════════════════════════════
    //                        Atlas 回调入口
    // ══════════════════════════════════════════════════════════════
    /**
     * Atlas 在我们中标后调这个。
     *
     * @param solverOpFrom        本次出价用的 solver 身份(押金记在它名下)
     * @param executionEnvironment Atlas 为本次执行创建的沙箱,**中标价要付给它**
     * @param bidToken            出价币种。实测 SVR 上是 address(0) = 原生币
     * @param bidAmount           我们答应交出去的钱
     * @param solverOpData        我们自己编码的清算参数
     */
    function atlasSolverCall(
        address solverOpFrom,
        address executionEnvironment,
        address bidToken,
        uint256 bidAmount,
        bytes calldata solverOpData,
        bytes calldata /* forwardedData */
    ) external payable {
        // ① 只有 Atlas 能调。没有这一行,任何人都能让本合约执行任意 swap。
        if (msg.sender != ATLAS) revert NotAtlas();

        // ② 出价身份必须是我们自己。
        //    押金是记在 solverFrom 名下的 —— 不校验的话,别人可以用
        //    他们的 solverOp 触发我们的合约、花我们的钱。
        if (solverOpFrom != OWNER) revert WrongSolverIdentity();

        // ③ 出价封顶。程序算错或者被喂了坏价格时的最后一道闸。
        if (bidAmount > maxBidWei) revert BidTooHigh(bidAmount, maxBidWei);

        // ④ 目前只处理原生币出价(实测 SVR 的 bidToken 就是 address(0))。
        //    遇到别的币种直接 revert,而不是"猜一个处理方式"。
        if (bidToken != address(0)) revert UnsupportedBidToken(bidToken);

        LiqParams memory p = abi.decode(solverOpData, (LiqParams));
        if (!routerAllowed[p.swapTarget]) revert RouterNotAllowed();

        // ⑤ 借钱去清算。回调在 executeOperation 里。
        IAavePool(AAVE_POOL).flashLoanSimple(
            address(this),
            p.debtAsset,
            p.debtToCover,
            solverOpData,
            0
        );

        // ⑥ 付中标价给执行环境。
        //    注:这里用合约里预存的原生币付。收益留在 debtAsset,
        //    由 owner 定期再平衡 —— 这是**刻意的简化**,避免在
        //    metacall 里再插一次 swap 增加失败面。
        (bool ok, ) = executionEnvironment.call{value: bidAmount}("");
        if (!ok) revert BidTransferFailed();

        // ⑦ 结清欠 Atlas 的 gas。文档要求必须在执行结束前调用。
        IAtlas(ATLAS).reconcile(maxGasSpendWei);

        emit Liquidated(p.user, p.collateralAsset, 0, 0, bidAmount);
    }

    // ══════════════════════════════════════════════════════════════
    //                     Aave 闪电贷回调
    // ══════════════════════════════════════════════════════════════
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool) {
        // 只接受 Aave Pool 的回调,且必须是我们自己发起的。
        if (msg.sender != AAVE_POOL) revert NotAtlas();
        if (initiator != address(this)) revert NotSelf();

        LiqParams memory p = abi.decode(params, (LiqParams));

        // ① 还债换抵押物
        IERC20(asset).approve(AAVE_POOL, amount);
        uint256 collBefore = IERC20(p.collateralAsset).balanceOf(address(this));

        IAavePool(AAVE_POOL).liquidationCall(
            p.collateralAsset,
            p.debtAsset,
            p.user,
            p.debtToCover,
            false                       // 要底层资产,不要 aToken
        );

        uint256 collOut = IERC20(p.collateralAsset).balanceOf(address(this)) - collBefore;

        // ★ 滑点保护 1 ——————————————————————————————————————
        // 实际拿到的抵押物少于预期就整笔回滚。
        // 会发生这种情况是因为:清算被别人抢先了一部分、
        // closeFactor 限制、或者我们的估值本身就错了。
        if (collOut < p.minCollateralOut) {
            revert CollateralShortfall(collOut, p.minCollateralOut);
        }

        // ② 把抵押物换回债务资产
        uint256 debtBefore = IERC20(asset).balanceOf(address(this));
        IERC20(p.collateralAsset).approve(p.swapTarget, collOut);

        (bool ok, ) = p.swapTarget.call(p.swapData);
        if (!ok) revert SwapFailed();

        // 用完就清授权,不留敞口
        IERC20(p.collateralAsset).approve(p.swapTarget, 0);

        uint256 debtBack = IERC20(asset).balanceOf(address(this)) - debtBefore;

        // ★ 滑点保护 2 ——————————————————————————————————————
        // 换回来的钱少于预期就整笔回滚。
        // **这正是我们反编译的那个 bot 缺的东西。**
        if (debtBack < p.minDebtBack) {
            revert ProceedsShortfall(debtBack, p.minDebtBack);
        }

        // ③ 还闪电贷
        IERC20(asset).approve(AAVE_POOL, amount + premium);
        return true;
    }

    // ══════════════════════════════════════════════════════════════
    //                          管理
    // ══════════════════════════════════════════════════════════════
    function setRouter(address router, bool allowed) external onlyOwner {
        routerAllowed[router] = allowed;
        emit Configured(router, allowed);
    }

    function setCaps(uint256 maxBid_, uint256 maxGasSpend_) external onlyOwner {
        maxBidWei = maxBid_;
        maxGasSpendWei = maxGasSpend_;
    }

    /// 取回资产。**学习用合约必须留这个** —— 出问题时能把钱拿回来。
    function rescue(address token, uint256 amount) external onlyOwner {
        if (token == address(0)) {
            (bool ok, ) = OWNER.call{value: amount}("");
            if (!ok) revert BidTransferFailed();
        } else {
            IERC20(token).transfer(OWNER, amount);
        }
    }
}
