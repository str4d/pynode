
#
# ChainDb.py - Bitcoin blockchain database
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

import string
import cStringIO
import leveldb
import io
import os
import time
from decimal import Decimal
from Cache import Cache
from gevent.coros import BoundedSemaphore
import bitcoin
from bitcoin.core import *
from bitcoin.core.scripteval import VerifySignature
from bitcoin.core.serialize import *
from bitcoin.messages import MsgSerializable, msg_block



def tx_blk_cmp(a, b):
	if a.dFeePerKB != b.dFeePerKB:
		return int(a.dFeePerKB - b.dFeePerKB)
	return int(a.dPriority - b.dPriority)

def block_value(height, fees):
	subsidy = 50 * COIN
	subsidy >>= (height / 210000)
	return subsidy + fees

class TxIdx(object):
	def __init__(self, blkhash=b'\x00'*32, spentmask=0L):
		self.blkhash = blkhash
		self.spentmask = spentmask


class BlkMeta(object):
	def __init__(self):
		self.height = -1
		self.work = 0L

	def deserialize(self, s):
		l = s.split()
		if len(l) < 2:
			raise RuntimeError
		self.height = int(l[0])
		self.work = long(l[1], 16)

	def serialize(self):
		r = str(self.height) + ' ' + hex(self.work)
		return r

	def __repr__(self):
		return "BlkMeta(height %d, work %x)" % (self.height, self.work)


class HeightIdx(object):
	def __init__(self):
		self.blocks = []

	def deserialize(self, s):
		self.blocks = []
		l = s.split()
		for hashstr in l:
			self.blocks.append(lx(hashstr))

	def serialize(self):
		l = []
		for blkhash in self.blocks:
			l.append(b2lx(blkhash))
		return ' '.join(l)

	def __repr__(self):
		return "HeightIdx(blocks=%s)" % (self.serialize(),)


class DbLock(object):
	def __init__(self, db):
		self.db = db
		self.lock = BoundedSemaphore(1)

	def Get(self, key):
		try:
			self.lock.acquire()
			return self.db.Get(key)
		finally:
			self.lock.release()

	def Put(self, key, value):
		try:
			self.lock.acquire()
			self.db.Put(key, value)
		finally:
			self.lock.release()

	def Write(self, batch):
		try:
			self.lock.acquire()
			self.db.Write(batch)
		finally:
			self.lock.release()

class ChainDbLock(object):
	def __init__(self, chaindb):
		self._chaindb = chaindb
		self.lock = BoundedSemaphore(1)

	def acquire(self):
		self.lock.acquire()
		return self._chaindb

	def release(self):
		self.lock.release()

	def __getattr__(self, attr):
		try:
			self.lock.acquire()
			return getattr(self._chaindb, attr)
		finally:
			self.lock.release()


class ChainDb(object):
	def __init__(self, settings, datadir, log, mempool,
		     readonly=False, fast_dbm=False):
		self.settings = settings
		self.log = log
		self.mempool = mempool
		self.readonly = readonly
		self.fast_dbm = fast_dbm
		self.blk_cache = Cache(500)
		self.orphans = {}
		self.orphan_deps = {}

		# LevelDB to hold:
		#    tx:*      transaction outputs
		#    misc:*    state
		#    height:*  list of blocks at height h
		#    blkmeta:* block metadata
		#    blocks:*  block seek point in stream
		self.blk_write = io.BufferedWriter(io.FileIO(datadir + '/blocks.dat','ab'))
		self.blk_read = io.BufferedReader(io.FileIO(datadir + '/blocks.dat','rb'))
		self.db = DbLock(leveldb.LevelDB(datadir + '/leveldb'))

		try:
			self.db.Get('misc:height')
		except KeyError:
			self.log.write("INITIALIZING EMPTY BLOCKCHAIN DATABASE")
			batch = leveldb.WriteBatch()
			batch.Put('misc:height', str(-1))
			batch.Put('misc:msg_start', bitcoin.params.MESSAGE_START)
			batch.Put('misc:tophash', b2lx(b'\x00'*32))
			batch.Put('misc:total_work', hex(0L))
			self.db.Write(batch)

		try:
			start = self.db.Get('misc:msg_start')
			if start != bitcoin.params.MESSAGE_START: raise KeyError
		except KeyError:
			self.log.write("Database magic number mismatch. Data corruption or incorrect network?")
			raise RuntimeError

	def puttxidx(self, txhash, txidx, batch=None):
		ser_txhash = b2lx(txhash)


		try:
			self.db.Get('tx:'+ser_txhash)
			old_txidx = self.gettxidx(txhash)
			self.log.write("WARNING: overwriting duplicate TX %s, height %d, oldblk %s, oldspent %x, newblk %s" % (b2lx(txhash), self.getheight(), b2lx(old_txidx.blkhash), old_txidx.spentmask, b2lx(txidx.blkhash)))
		except KeyError:
			pass
		batch = self.db if batch is not None else batch
		batch.Put('tx:'+ser_txhash, b2lx(txidx.blkhash) + ' ' +
					       hex(txidx.spentmask))

		return True

	def gettxidx(self, txhash):
		ser_txhash = b2lx(txhash)
		try:
			ser_value = self.db.Get('tx:'+ser_txhash)
		except KeyError:
			return None

		pos = string.find(ser_value, ' ')

		txidx = TxIdx()
		txidx.blkhash = lx(ser_value[:pos])
		txidx.spentmask = long(ser_value[pos+1:], 16)

		return txidx

	def gettx(self, txhash):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return None

		block = self.getblock(txidx.blkhash)
		for tx in block.vtx:
			if tx.GetHash() == txhash:
				return tx

		self.log.write("ERROR: Missing TX %s in block %s" % (b2lx(txhash), b2lx(txidx.blkhash)))
		return None

	def haveblock(self, blkhash, checkorphans):
		if self.blk_cache.exists(blkhash):
			return True
		if checkorphans and blkhash in self.orphans:
			return True
		ser_hash = b2lx(blkhash)
		try: 
			self.db.Get('blocks:'+ser_hash)
			return True
		except KeyError:
			return False

	def have_prevblock(self, block):
		if self.getheight() < 0 and block.GetHash() == bitcoin.params.GENESIS_BLOCK.GetHash():
			return True
		if self.haveblock(block.hashPrevBlock, False):
			return True
		return False

	def getblock(self, blkhash):
		block = self.blk_cache.get(blkhash)
		if block is not None:
			return block

		ser_hash = b2lx(blkhash)
		try:
			# Lookup the block index, seek in the file
			fpos = long(self.db.Get('blocks:'+ser_hash))
			self.blk_read.seek(fpos)

			# read and decode "block" msg
			msg = MsgSerializable.stream_deserialize(self.blk_read)
			if msg is None:
				return None
			block = msg.block
		except KeyError:
			return None

		self.blk_cache.put(blkhash, block)

		return block

	def spend_txout(self, txhash, n_idx, batch=None):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return False

		txidx.spentmask |= (1L << n_idx)
		self.puttxidx(txhash, txidx, batch)

		return True

	def clear_txout(self, txhash, n_idx, batch=None):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return False

		txidx.spentmask &= ~(1L << n_idx)
		self.puttxidx(txhash, txidx, batch)

		return True

	def unique_outpts(self, block):
		outpts = {}
		txmap = {}
		for tx in block.vtx:
			if tx.is_coinbase:
				continue
			txmap[tx.GetHash()] = tx
			for txin in tx.vin:
				v = (txin.prevout.hash, txin.prevout.n)
				if v in outs:
					return None

				outpts[v] = False

		return (outpts, txmap)

	def txout_spent(self, txout):
		txidx = self.gettxidx(txout.hash)
		if txidx is None:
			return None

		if txout.n > 100000:	# outpoint index sanity check
			return None

		if txidx.spentmask & (1L << txout.n):
			return True

		return False

	def spent_outpts(self, block):
		# list of outpoints this block wants to spend
		l = self.unique_outpts(block)
		if l is None:
			return None
		outpts = l[0]
		txmap = l[1]
		spendlist = {}

		# pass 1: if outpoint in db, make sure it is unspent
		for k in outpts.iterkeys():
			outpt = COutPoint()
			outpt.hash = k[0]
			outpt.n = k[1]
			rc = self.txout_spent(outpt)
			if rc is None:
				continue
			if rc:
				return None

			outpts[k] = True	# skip in pass 2

		# pass 2: remaining outpoints must exist in this block
		for k, v in outpts.iteritems():
			if v:
				continue

			if k[0] not in txmap:	# validate txout hash
				return None

			tx = txmap[k[0]]	# validate txout index (n)
			if k[1] >= len(tx.vout):
				return None

			# outpts[k] = True	# not strictly necessary

		return outpts.keys()

	def tx_signed(self, tx, block, check_mempool):
		for i in xrange(len(tx.vin)):
			txin = tx.vin[i]

			# search database for dependent TX
			txfrom = self.gettx(txin.prevout.hash)

			# search block for dependent TX
			if txfrom is None and block is not None:
				for blktx in block.vtx:
					if blktx.GetHash() == txin.prevout.hash:
						txfrom = blktx
						break

			# search mempool for dependent TX
			if txfrom is None and check_mempool:
				try:
					txfrom = self.mempool.pool[txin.prevout.hash]
				except:
					self.log.write("TX %s/%d no-dep %s" %
							(b2lx(tx.GetHash()), i,
							 b2lx(txin.prevout.hash)))
					return False
			if txfrom is None:
				self.log.write("TX %s/%d no-dep %s" %
						(b2lx(tx.GetHash()), i,
						 b2lx(txin.prevout.hash)))
				return False

			if False and not VerifySignature(txfrom, tx, i):
				self.log.write("TX %s/%d sigfail" %
						(b2lx(tx.GetHash()), i))
				return False

		return True

	def tx_is_orphan(self, tx):
		try:
			CheckTransaction(tx)
		except CheckTransactionError:
			return None

		for txin in tx.vin:
			rc = self.txout_spent(txin.prevout)
			if rc is None:		# not found: orphan
				try:
					txfrom = self.mempool.pool[txin.prevout.hash]
				except:
					return True
				if txin.prevout.n >= len(txfrom.vout):
					return None
			if rc is True:		# spent? strange
				return None

		return False

	def connect_block(self, ser_hash, block, blkmeta):
		# check TX connectivity
		outpts = self.spent_outpts(block)
		if outpts is None:
			self.log.write("Unconnectable block %s" % (b2lx(block.GetHash()), ))
			return False

		# verify script signatures
		if ('nosig' not in self.settings):
			for tx in block.vtx:
				if tx.is_coinbase():
					continue

				if not self.tx_signed(tx, block, False):
					self.log.write("Invalid signature in block %s" % (b2lx(block.GetHash()), ))
					return False

		# update database pointers for best chain
		batch = leveldb.WriteBatch()
		batch.Put('misc:total_work', hex(blkmeta.work))
		batch.Put('misc:height', str(blkmeta.height))
		batch.Put('misc:tophash', ser_hash)

		self.log.write("ChainDb: height %d, block %s" % (
				blkmeta.height, b2lx(block.GetHash())))

		# all TX's in block are connectable; index
		neverseen = 0
		for tx in block.vtx:
			if not self.mempool.remove(tx.GetHash()):
				neverseen += 1

			txidx = TxIdx(block.GetHash())
			if not self.puttxidx(tx.GetHash(), txidx, batch):
				self.log.write("TxIndex failed %s" % (b2lx(tx.GetHash()),))
				return False

		self.log.write("MemPool: blk.vtx.sz %d, neverseen %d, poolsz %d" % (len(block.vtx), neverseen, self.mempool.size()))

		# mark deps as spent
		for outpt in outpts:
			self.spend_txout(outpt[0], outpt[1], batch)

		self.db.Write(batch)
		return True

	def disconnect_block(self, block):
		ser_prevhash = b2lx(block.hashPrevBlock)
		prevmeta = BlkMeta()
		prevmeta.deserialize(self.db.Get('blkmeta:'+ser_prevhash))

		tup = self.unique_outpts(block)
		if tup is None:
			return False

		outpts = tup[0]

		# mark deps as unspent
		batch = leveldb.WriteBatch()
		for outpt in outpts:
			self.clear_txout(outpt[0], outpt[1], batch)

		# update tx index and memory pool
		for tx in block.vtx:
			ser_hash = b2lx(tx.GetHash())
			try:
				batch.Delete('tx:'+ser_hash)
			except KeyError:
				pass

			if not tx.is_coinbase():
				self.mempool.add(tx)

		# update database pointers for best chain
		batch.Put('misc:total_work', hex(prevmeta.work))
		batch.Put('misc:height', str(prevmeta.height))
		batch.Put('misc:tophash', ser_prevhash)
		self.db.Write(batch)

		self.log.write("ChainDb(disconn): height %d, block %s" % (
				prevmeta.height, b2lx(block.hashPrevBlock)))

		return True

	def getblockhash(self, height):
		heightidx = HeightIdx()
		heightstr = str(height)
		try:
			heightidx.deserialize(self.db.Get('height:'+heightstr))
		except KeyError:
			pass
		return heightidx.blocks[0]

	def getblockmeta(self, blkhash):
		ser_hash = b2lx(blkhash)
		try:
			meta = BlkMeta()
			meta.deserialize(self.db.Get('blkmeta:'+ser_hash))
		except KeyError:
			return None

		return meta
	
	def getblockheight(self, blkhash):
		meta = self.getblockmeta(blkhash)
		if meta is None:
			return -1

		return meta.height

	def reorganize(self, new_best_blkhash):
		self.log.write("REORGANIZE")

		conn = []
		disconn = []

		old_best_blkhash = self.gettophash()
		fork = old_best_blkhash
		longer = new_best_blkhash
		while fork != longer:
			while (self.getblockheight(longer) >
			       self.getblockheight(fork)):
				block = self.getblock(longer)
				conn.append(block)

				longer = block.hashPrevBlock
				if longer == b'\x00'*32:
					return False

			if fork == longer:
				break

			block = self.getblock(fork)
			disconn.append(block)

			fork = block.hashPrevBlock
			if fork == b'\x00'*32:
				return False

		self.log.write("REORG disconnecting top hash %s" % (b2lx(old_best_blkhash),))
		self.log.write("REORG connecting new top hash %s" % (b2lx(new_best_blkhash),))
		self.log.write("REORG chain union point %s" % (b2lx(fork),))
		self.log.write("REORG disconnecting %d blocks, connecting %d blocks" % (len(disconn), len(conn)))

		for block in disconn:
			if not self.disconnect_block(block):
				return False

		for block in conn:
			if not self.connect_block(b2lx(block.GetHash()),
				  block, self.getblockmeta(block.GetHash())):
				return False

		self.log.write("REORGANIZE DONE")
		return True

	def set_best_chain(self, ser_prevhash, ser_hash, block, blkmeta):
		# the easy case, extending current best chain
		if (blkmeta.height == 0 or
		    self.db.Get('misc:tophash') == ser_prevhash):
			return self.connect_block(ser_hash, block, blkmeta)

		# switching from current chain to another, stronger chain
		return self.reorganize(block.GetHash())

	def putoneblock(self, block):
		try:
			CheckBlock(block)
		except CheckBlockError:
			self.log.write("Invalid block %s" % (b2lx(block.GetHash()), ))
			return False

		if not self.have_prevblock(block):
			self.orphans[block.GetHash()] = True
			self.orphan_deps[block.hashPrevBlock] = block
			self.log.write("Orphan block %s (%d orphans)" % (b2lx(block.GetHash()), len(self.orphan_deps)))
			return False

		top_height = self.getheight()
		top_work = long(self.db.Get('misc:total_work'), 16)

		# read metadata for previous block
		prevmeta = BlkMeta()
		if top_height >= 0:
			ser_prevhash = b2lx(block.hashPrevBlock)
			prevmeta.deserialize(self.db.Get('blkmeta:'+ser_prevhash))
		else:
			ser_prevhash = ''

		batch = leveldb.WriteBatch()

		# build network "block" msg, as canonical disk storage form
		msg = msg_block()
		msg.block = block
		msg_data = msg.to_bytes()

		# write "block" msg to storage
		fpos = self.blk_write.tell()
		self.blk_write.write(msg_data)
		self.blk_write.flush()

		# add index entry
		ser_hash = b2lx(block.GetHash())
		batch.Put('blocks:'+ser_hash, str(fpos))

		# store metadata related to this block
		blkmeta = BlkMeta()
		blkmeta.height = prevmeta.height + 1
		blkmeta.work = (prevmeta.work +
				uint256_from_compact(block.nBits))
		batch.Put('blkmeta:'+ser_hash, blkmeta.serialize())

		# store list of blocks at this height
		heightidx = HeightIdx()
		heightstr = str(blkmeta.height)
		try:
			heightidx.deserialize(self.db.Get('height:'+heightstr))
		except KeyError:
			pass
		heightidx.blocks.append(block.GetHash())

		batch.Put('height:'+heightstr, heightidx.serialize())
		self.db.Write(batch)

		# if chain is not best chain, proceed no further
		if (blkmeta.work <= top_work):
			self.log.write("ChainDb: height %d (weak), block %s" % (blkmeta.height, b2lx(block.GetHash())))
			return True

		# update global chain pointers
		if not self.set_best_chain(ser_prevhash, ser_hash,
					   block, blkmeta):
			return False

		return True

	def putblock(self, block):
		if self.haveblock(block.GetHash(), True):
			self.log.write("Duplicate block %s submitted" % (b2lx(block.GetHash()), ))
			return False

		if not self.putoneblock(block):
			return False

		blkhash = block.GetHash()
		while blkhash in self.orphan_deps:
			block = self.orphan_deps[blkhash]
			if not self.putoneblock(block):
				return True

			del self.orphan_deps[blkhash]
			del self.orphans[block.GetHash()]

			blkhash = block.GetHash()

		return True

	def locate(self, locator):
		for hash in locator.vHave:
			ser_hash = b2lx(hash)
			try:
				blkmeta = BlkMeta()
				blkmeta.deserialize(self.db.Get('blkmeta:'+ser_hash))
				return blkmeta
			except KeyError:
				pass
		blkmeta = BlkMeta()
		blkmeta.height = 0
		return blkmeta

	def getheight(self):
		return int(self.db.Get('misc:height'))

	def gettophash(self):
		return lx(self.db.Get('misc:tophash'))

	def loadfile(self, filename):
		fd = os.open(filename, os.O_RDONLY)
		self.log.write("IMPORTING DATA FROM " + filename)
		buf = ''
		wanted = 4096
		while True:
			if wanted > 0:
				if wanted < 4096:
					wanted = 4096
				s = os.read(fd, wanted)
				if len(s) == 0:
					break

				buf += s
				wanted = 0

			buflen = len(buf)
			startpos = string.find(buf, bitcoin.params.MESSAGE_START)
			if startpos < 0:
				wanted = 8
				continue

			sizepos = startpos + 4
			blkpos = startpos + 8
			if blkpos > buflen:
				wanted = 8
				continue

			blksize = struct.unpack("<i", buf[sizepos:blkpos])[0]
			if (blkpos + blksize) > buflen:
				wanted = 8 + blksize
				continue

			ser_blk = buf[blkpos:blkpos+blksize]
			buf = buf[blkpos+blksize:]

			f = cStringIO.StringIO(ser_blk)
			block = CBlock()
			block.deserialize(f)

			self.putblock(block)

	def newblock_txs(self):
		txlist = []
		for tx in self.mempool.pool.itervalues():

			# query finalized, non-coinbase mempool tx's
			if tx.is_coinbase() or not tx.is_final():
				continue

			# iterate through inputs, calculate total input value
			valid = True
			nValueIn = 0
			nValueOut = 0
			dPriority = Decimal(0)

			for tin in tx.vin:
				in_tx = self.gettx(tin.prevout.hash)
				if (in_tx is None or
				    tin.prevout.n >= len(in_tx.vout)):
					valid = False
				else:
					v = in_tx.vout[tin.prevout.n].nValue
					nValueIn += v
					dPriority += Decimal(v * 1)

			if not valid:
				continue

			# iterate through outputs, calculate total output value
			for txout in tx.vout:
				nValueOut += txout.nValue

			# calculate fees paid, if any
			tx.nFeesPaid = nValueIn - nValueOut
			if tx.nFeesPaid < 0:
				continue

			# calculate fee-per-KB and priority
			tx.ser_size = len(tx.serialize())

			dPriority /= Decimal(tx.ser_size)

			tx.dFeePerKB = (Decimal(tx.nFeesPaid) /
					(Decimal(tx.ser_size) / Decimal(1000)))
			if tx.dFeePerKB < Decimal(50000):
				tx.dFeePerKB = Decimal(0)
			tx.dPriority = dPriority

			txlist.append(tx)

		# sort list by fee-per-kb, then priority
		sorted_txlist = sorted(txlist, cmp=tx_blk_cmp, reverse=True)

		# build final list of transactions.  thanks to sort
		# order above, we add TX's to the block in the
		# highest-fee-first order.  free transactions are
		# then appended in order of priority, until
		# free_bytes is exhausted.
		txlist = []
		txlist_bytes = 0
		free_bytes = 50000
		while len(sorted_txlist) > 0:
			tx = sorted_txlist.pop()
			if txlist_bytes + tx.ser_size > (900 * 1000):
				continue

			if tx.dFeePerKB > 0:
				txlist.append(tx)
				txlist_bytes += tx.ser_size
			elif free_bytes >= tx.ser_size:
				txlist.append(tx)
				txlist_bytes += tx.ser_size
				free_bytes -= tx.ser_size
		
		return txlist

	def newblock(self):
		tophash = self.gettophash()
		prevblock = self.getblock(tophash)
		if prevblock is None:
			return None

		# obtain list of candidate transactions for a new block
		total_fees = 0
		txlist = self.newblock_txs()
		for tx in txlist:
			total_fees += tx.nFeesPaid

		#
		# build coinbase
		#
		txin = CTxIn()
		txin.prevout.set_null()
		# FIXME: txin.scriptSig

		txout = CTxOut()
		txout.nValue = block_value(self.getheight(), total_fees)
		# FIXME: txout.scriptPubKey

		coinbase = CTransaction()
		coinbase.vin.append(txin)
		coinbase.vout.append(txout)

		#
		# build block
		#
		block = CBlock()
		block.hashPrevBlock = tophash
		block.nTime = int(time.time())
		block.nBits = prevblock.nBits	# TODO: wrong
		block.vtx.append(coinbase)
		block.vtx.extend(txlist)
		block.hashMerkleRoot = block.calc_merkle()

		return block

