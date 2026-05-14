///////////////////////////////////////////////////////////////////////////////////
// Copyright (C) 2017-2020 Edouard Griffiths, F4EXB <f4exb06@gmail.com>          //
// Copyright (C) 2020-26 Jon Beniston, M7RCE <jon@beniston.com>                  //
//                                                                               //
// This program is free software; you can redistribute it and/or modify          //
// it under the terms of the GNU General Public License as published by          //
// the Free Software Foundation as version 3 of the License, or                  //
// (at your option) any later version.                                           //
//                                                                               //
// This program is distributed in the hope that it will be useful,               //
// but WITHOUT ANY WARRANTY; without even the implied warranty of                //
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the                  //
// GNU General Public License V3 for more details.                               //
//                                                                               //
// You should have received a copy of the GNU General Public License             //
// along with this program. If not, see <http://www.gnu.org/licenses/>.          //
///////////////////////////////////////////////////////////////////////////////////

#include <errno.h>
#include <algorithm>
#include <chrono>

#include <QDebug>

#include "dsp/samplesourcefifo.h"

#include "usrpoutputthread.h"

USRPOutputThread::USRPOutputThread(uhd::tx_streamer::sptr stream,
                                   size_t bufSamples,
                                   SampleSourceFifo* sampleFifo,
                                   uhd::usrp::multi_usrp::sptr dev,
                                   size_t numChannels,
                                   quint32 maxUnderflowCount,
                                   QObject* parent) :
    QThread(parent),
    m_running(false),
    m_packets(0),
    m_underflows(0),
    m_droppedPackets(0),
    m_stream(stream),
    m_bufSamples(bufSamples),
    m_sampleFifo(sampleFifo),
    m_log2Interp(0),
    m_dev(dev),
    m_numChannels(std::max<size_t>(1, numChannels)),
    m_maxUnderflowCount(maxUnderflowCount),
    m_zeroBuf(nullptr),
    m_burstActive(false),
    m_consecutiveUnderflows(0)
{
    m_buf = new qint16[2 * bufSamples];
    std::fill(m_buf, m_buf + 2 * bufSamples, 0);

    if (m_numChannels > 1) {
        m_zeroBuf = new qint16[2 * bufSamples];
        std::fill(m_zeroBuf, m_zeroBuf + 2 * bufSamples, 0);
    }
}

USRPOutputThread::~USRPOutputThread()
{
    stopWork();
    delete[] m_buf;
    delete[] m_zeroBuf;
}

void USRPOutputThread::startWork()
{
    if (m_running) return;

    m_packets = 0;
    m_underflows = 0;
    m_droppedPackets = 0;
    m_burstActive = false;
    m_consecutiveUnderflows = 0;

    m_startWaitMutex.lock();
    start();
    while (!m_running) {
        m_startWaiter.wait(&m_startWaitMutex, 100);
    }
    m_startWaitMutex.unlock();
}

void USRPOutputThread::stopWork()
{
    if (!m_running) return;

    m_running = false;
    wait();

    qDebug("USRPOutputThread::stopWork: stream stopped");
}

void USRPOutputThread::sendEndOfBurst()
{
    if (!m_burstActive) return;
    m_burstActive = false;

    try {
        uhd::tx_metadata_t md;
        md.start_of_burst = false;
        md.end_of_burst   = true;
        md.has_time_spec  = false;

        const void* nullBufs[2] = {m_buf, m_zeroBuf ? m_zeroBuf : m_buf};
        const void* const* sendBufs = m_numChannels > 1 ? nullBufs : (const void* const*)(&nullBufs[0]);
        m_stream->send(sendBufs, 0, md, 0.01);
    } catch (std::exception& e) {
        qDebug() << "USRPOutputThread::sendEndOfBurst: exception: " << e.what();
    }
}

void USRPOutputThread::setLog2Interpolation(unsigned int log2_interp)
{
    m_log2Interp = log2_interp;
}

void USRPOutputThread::run()
{
    uhd::tx_metadata_t md;

    m_running = true;
    m_startWaiter.wakeAll();

    qDebug("USRPOutputThread::run");

    while (m_running)
    {
        std::fill(m_buf, m_buf + 2 * m_bufSamples, 0);

        qint32 writtenSamples = callback(m_buf, m_bufSamples);

        if (writtenSamples <= 0) {
            QThread::usleep(100);
            continue;
        }

        if (!m_burstActive) {
            m_burstActive = true;
            m_consecutiveUnderflows = 0;
            md.start_of_burst = true;
            md.end_of_burst   = false;
            md.has_time_spec  = false;
        } else {
            md.start_of_burst = false;
            md.end_of_burst   = false;
            md.has_time_spec  = false;
        }

        try
        {
            size_t sentSamples = m_stream->send(m_buf, writtenSamples, md, 0.01);
            m_packets++;
        }
        catch (std::exception& e)
        {
            qDebug() << "USRPOutputThread::run: exception: " << e.what();
            if (m_burstActive) {
                sendEndOfBurst();
            }
            break;
        }
    }

    if (m_burstActive) {
        sendEndOfBurst();
    }

    m_running = false;
    qDebug("USRPOutputThread::run: exit");
}

qint32 USRPOutputThread::callback(qint16* buf, qint32 len)
{
    const unsigned int interpolationFactor = 1U << m_log2Interp;
    SampleVector& data = m_sampleFifo->getData();
    unsigned int iPart1Begin, iPart1End, iPart2Begin, iPart2End;

    m_sampleFifo->read(static_cast<unsigned int>(len)/interpolationFactor, iPart1Begin, iPart1End, iPart2Begin, iPart2End);

    if (iPart1Begin != iPart1End) {
        callbackPart(buf, data, iPart1Begin, iPart1End);
    }

    const unsigned int shift = (iPart1End - iPart1Begin) * interpolationFactor;

    if (iPart2Begin != iPart2End) {
        callbackPart(buf + 2 * shift, data, iPart2Begin, iPart2End);
    }

    return ((iPart1End - iPart1Begin) + (iPart2End - iPart2Begin)) * interpolationFactor;
}

void USRPOutputThread::callbackPart(qint16* buf, SampleVector& data, unsigned int iBegin, unsigned int iEnd)
{
    SampleVector::iterator beginRead = data.begin() + iBegin;
    const int len = 2 * (iEnd - iBegin) * (1 << m_log2Interp);

    if (m_log2Interp == 0)
    {
        m_interpolators.interpolate1(&beginRead, buf, len);
    }
    else
    {
        switch (m_log2Interp)
        {
        case 1:
            m_interpolators.interpolate2_cen(&beginRead, buf, len);
            break;
        case 2:
            m_interpolators.interpolate4_cen(&beginRead, buf, len);
            break;
        case 3:
            m_interpolators.interpolate8_cen(&beginRead, buf, len);
            break;
        case 4:
            m_interpolators.interpolate16_cen(&beginRead, buf, len);
            break;
        case 5:
            m_interpolators.interpolate32_cen(&beginRead, buf, len);
            break;
        case 6:
            m_interpolators.interpolate64_cen(&beginRead, buf, len);
            break;
        default:
            break;
        }
    }
}

void USRPOutputThread::getStreamStatus(bool& active, quint32& underflows, quint32& droppedPackets)
{
    uhd::async_metadata_t md;

    if (m_stream->recv_async_msg(md))
    {
        if ((md.event_code & uhd::async_metadata_t::event_code_t::EVENT_CODE_UNDERFLOW)
            || (md.event_code & uhd::async_metadata_t::event_code_t::EVENT_CODE_UNDERFLOW_IN_PACKET)) {
            m_underflows++;
        }
        if ((md.event_code & uhd::async_metadata_t::event_code_t::EVENT_CODE_SEQ_ERROR)
            || (md.event_code & uhd::async_metadata_t::event_code_t::EVENT_CODE_SEQ_ERROR_IN_BURST)) {
            m_droppedPackets++;
        }
    }
    active = m_packets > 0;
    underflows = m_underflows;
    droppedPackets = m_droppedPackets;
}
